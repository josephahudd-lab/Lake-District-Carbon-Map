use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use indicatif::{ProgressBar, ProgressStyle};
use las::point::Classification;
use las::Reader;
use rayon::prelude::*;
use rustc_hash::FxHashMap;

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq)]
enum HagMode {
    /// Auto-detect: if raw file has Ground points, builds fast in-memory DTM on the fly.
    /// If extra_bytes has HAG, reads that; else falls back to Z.
    Auto,
    /// High-speed in-memory DTM construction with bilinear interpolation directly from raw LAZ
    OnTheFlyDtm,
    /// Height is in point.z (pre-normalised)
    Z,
    /// Height is in point.extra_bytes as a 4-byte float32
    ExtraBytes,
}

#[derive(Parser, Debug)]
#[command(
    name = "carbon_lidar",
    author = "Lake District Carbon Project",
    version = "0.2.0",
    about = "Blazing fast LiDAR point cloud processor with on-the-fly DTM normalisation"
)]
struct Args {
    /// Input .laz file or directory containing .laz files (can be RAW or pre-normalised)
    #[arg(short, long)]
    input: PathBuf,

    /// Output directory for gridded CSV results
    #[arg(short, long)]
    output_dir: PathBuf,

    /// Grid cell resolution in metres (snapped to British National Grid EPSG:27700)
    #[arg(short, long, default_value_t = 20.0)]
    grid_size: f64,

    /// Internal DTM ground grid resolution in metres for on-the-fly normalisation
    #[arg(long, default_value_t = 2.5)]
    dtm_res: f64,

    /// Minimum vegetation height threshold in metres (HAG)
    #[arg(long, default_value_t = 3.0)]
    min_veg_height: f64,

    /// Minimum vegetation points required per cell to evaluate volume
    #[arg(long, default_value_t = 10)]
    min_veg_points: usize,

    /// Minimum permeability ratio (multi-return veg / total veg)
    #[arg(long, default_value_t = 0.05)]
    min_permeability: f64,

    /// Height Above Ground (HAG) reading mode
    #[arg(long, value_enum, default_value_t = HagMode::Auto)]
    hag_mode: HagMode,

    /// Number of worker threads (0 = all available CPU cores)
    #[arg(short, long, default_value_t = 0)]
    threads: usize,

    /// Also generate a combined single master CSV of all processed tiles
    #[arg(long, default_value_t = true)]
    combine_master: bool,
}

#[derive(Clone, Copy)]
struct CandidatePoint {
    dx: f32,
    dy: f32,
    z: f32,
    return_number: u8,
    number_of_returns: u8,
}

#[derive(Default)]
struct CellAccumulator {
    total_points: u32,
    single_returns: u32,
    veg_points: u32,
    veg_multi_returns: u32,
    veg_heights: Vec<f32>,
}

#[derive(Debug, Clone)]
pub struct GridMetric {
    pub easting: f64,
    pub northing: f64,
    pub h90: f64,
    pub volume_ha: f64,
}

/// In-memory Digital Terrain Model (DTM) for bilinear ground surface interpolation
struct GroundDtm {
    min_x: f64,
    min_y: f64,
    res: f64,
    cols: usize,
    rows: usize,
    grid: Vec<f32>,
}

impl GroundDtm {
    fn new(min_x: f64, max_x: f64, min_y: f64, max_y: f64, res: f64) -> Self {
        let cols = ((max_x - min_x) / res).ceil() as usize + 2;
        let rows = ((max_y - min_y) / res).ceil() as usize + 2;
        let grid = vec![f32::NAN; cols * rows];
        Self {
            min_x,
            min_y,
            res,
            cols,
            rows,
            grid,
        }
    }

    #[inline(always)]
    fn add_ground_point(&mut self, x: f64, y: f64, z: f64) {
        let col = ((x - self.min_x) / self.res) as usize;
        let row = ((y - self.min_y) / self.res) as usize;
        if col < self.cols && row < self.rows {
            let idx = row * self.cols + col;
            let val = z as f32;
            if self.grid[idx].is_nan() || val < self.grid[idx] {
                self.grid[idx] = val;
            }
        }
    }

    /// Fast 3x3 iterative gap filling for unobserved ground cells (e.g. under dense canopy)
    fn fill_gaps(&mut self) {
        let cols = self.cols;
        let rows = self.rows;

        // Run 2 iterative passes of 3x3 local averaging for missing cells
        for _ in 0..2 {
            let mut filled_any = false;
            let old_grid = self.grid.clone();

            for r in 0..rows {
                let r_min = r.saturating_sub(1);
                let r_max = (r + 1).min(rows - 1);

                for c in 0..cols {
                    let idx = r * cols + c;
                    if old_grid[idx].is_nan() {
                        let c_min = c.saturating_sub(1);
                        let c_max = (c + 1).min(cols - 1);

                        let mut sum = 0.0f32;
                        let mut count = 0;

                        for nr in r_min..=r_max {
                            for nc in c_min..=c_max {
                                let n_idx = nr * cols + nc;
                                let v = old_grid[n_idx];
                                if !v.is_nan() {
                                    sum += v;
                                    count += 1;
                                }
                            }
                        }

                        if count > 0 {
                            self.grid[idx] = sum / (count as f32);
                            filled_any = true;
                        }
                    }
                }
            }

            if !filled_any {
                break;
            }
        }

        // Global fallback: any remaining NaNs (e.g. edge voids or lakes) take global median/average
        let valid_sum: f64 = self.grid.iter().filter(|v| !v.is_nan()).map(|&v| v as f64).sum();
        let valid_count = self.grid.iter().filter(|v| !v.is_nan()).count();
        let global_mean = if valid_count > 0 {
            (valid_sum / (valid_count as f64)) as f32
        } else {
            0.0f32
        };

        for v in self.grid.iter_mut() {
            if v.is_nan() {
                *v = global_mean;
            }
        }
    }

    /// Bilinear interpolation of ground elevation at (x, y)
    #[inline(always)]
    fn get_ground_z(&self, x: f64, y: f64) -> f32 {
        let gx = (x - self.min_x) / self.res - 0.5;
        let gy = (y - self.min_y) / self.res - 0.5;

        let c0 = gx.floor() as isize;
        let r0 = gy.floor() as isize;

        let fx = (gx - c0 as f64) as f32;
        let fy = (gy - r0 as f64) as f32;

        let c0_clamped = c0.clamp(0, self.cols as isize - 1) as usize;
        let c1_clamped = (c0 + 1).clamp(0, self.cols as isize - 1) as usize;
        let r0_clamped = r0.clamp(0, self.rows as isize - 1) as usize;
        let r1_clamped = (r0 + 1).clamp(0, self.rows as isize - 1) as usize;

        let z00 = self.grid[r0_clamped * self.cols + c0_clamped];
        let z10 = self.grid[r0_clamped * self.cols + c1_clamped];
        let z01 = self.grid[r1_clamped * self.cols + c0_clamped];
        let z11 = self.grid[r1_clamped * self.cols + c1_clamped];

        (1.0 - fx) * (1.0 - fy) * z00
            + fx * (1.0 - fy) * z10
            + (1.0 - fx) * fy * z01
            + fx * fy * z11
    }
}

/// Compute percentile from a sorted slice using linear interpolation (matching numpy.percentile)
fn linear_percentile(sorted: &[f32], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    if sorted.len() == 1 {
        return sorted[0] as f64;
    }
    let idx = (q / 100.0) * (sorted.len() - 1) as f64;
    let lo = idx.floor() as usize;
    let hi = (lo + 1).min(sorted.len() - 1);
    let frac = idx - lo as f64;
    sorted[lo] as f64 + frac * (sorted[hi] as f64 - sorted[lo] as f64)
}

fn process_single_tile(
    path: &Path,
    grid_size: f64,
    dtm_res: f64,
    min_veg_height: f64,
    min_veg_points: usize,
    min_permeability: f64,
    hag_mode: HagMode,
) -> Result<Vec<GridMetric>> {
    let mut reader = Reader::from_path(path)
        .with_context(|| format!("Failed to open LAS/LAZ file {:?}", path))?;

    let bounds = reader.header().bounds();
    let min_x = bounds.min.x;
    let min_y = bounds.min.y;

    // Check mode: if Auto, inspect first point to see if extra_bytes or if ground classification exists
    let mut use_dtm = hag_mode == HagMode::OnTheFlyDtm;
    let mut use_extra_bytes = hag_mode == HagMode::ExtraBytes;

    if hag_mode == HagMode::Auto {
        // Read first point to inspect characteristics
        if let Some(Ok(first_pt)) = reader.points().next() {
            if first_pt.extra_bytes.len() >= 4 {
                let sample_val = f32::from_le_bytes(first_pt.extra_bytes[0..4].try_into().unwrap());
                if sample_val.is_finite() && sample_val > -50.0 && sample_val < 500.0 {
                    use_extra_bytes = true;
                }
            }
        }
        // If not extra bytes, use on-the-fly DTM by default for raw un-normalised files
        if !use_extra_bytes {
            use_dtm = true;
        }
    }

    // Single streaming pass over all points!
    let mut pass_reader = Reader::from_path(path)?;
    let mut grid: FxHashMap<(i64, i64), CellAccumulator> = FxHashMap::default();
    let grid_step = grid_size.round() as i64;

    let mut dtm = if use_dtm {
        Some(GroundDtm::new(min_x, bounds.max.x, min_y, bounds.max.y, dtm_res))
    } else {
        None
    };

    let mut candidates: Vec<CandidatePoint> = Vec::new();

    for point_res in pass_reader.points() {
        let point = point_res?;
        let cell_x = (point.x / grid_size).floor() as i64 * grid_step;
        let cell_y = (point.y / grid_size).floor() as i64 * grid_step;

        let cell = grid.entry((cell_x, cell_y)).or_default();
        cell.total_points += 1;

        if point.return_number == 1 && point.number_of_returns == 1 {
            cell.single_returns += 1;
        }

        if let Some(ref mut ground_dtm) = dtm {
            if point.classification == Classification::Ground {
                ground_dtm.add_ground_point(point.x, point.y, point.z);
            } else {
                candidates.push(CandidatePoint {
                    dx: (point.x - min_x) as f32,
                    dy: (point.y - min_y) as f32,
                    z: point.z as f32,
                    return_number: point.return_number,
                    number_of_returns: point.number_of_returns,
                });
            }
        } else {
            let hag = if use_extra_bytes && point.extra_bytes.len() >= 4 {
                f32::from_le_bytes(point.extra_bytes[0..4].try_into().unwrap())
            } else {
                point.z as f32
            };
            if (hag as f64) > min_veg_height {
                cell.veg_points += 1;
                if point.number_of_returns > 1 {
                    cell.veg_multi_returns += 1;
                }
                cell.veg_heights.push(hag);
            }
        }
    }

    // If DTM was built, fill gaps and process buffered candidate points in RAM
    if let Some(mut ground_dtm) = dtm {
        ground_dtm.fill_gaps();

        for cand in candidates {
            let px = min_x + (cand.dx as f64);
            let py = min_y + (cand.dy as f64);
            let ground_z = ground_dtm.get_ground_z(px, py);
            let hag = cand.z - ground_z;

            if (hag as f64) > min_veg_height {
                let cell_x = (px / grid_size).floor() as i64 * grid_step;
                let cell_y = (py / grid_size).floor() as i64 * grid_step;
                let cell = grid.entry((cell_x, cell_y)).or_default();
                cell.veg_points += 1;
                if cand.number_of_returns > 1 {
                    cell.veg_multi_returns += 1;
                }
                cell.veg_heights.push(hag);
            }
        }
    }

    // Pass 3: Evaluate Holmgren volume regression per 20m cell
    let mut results = Vec::new();
    let half_grid = grid_size / 2.0;

    for ((cell_x, cell_y), mut cell) in grid {
        if (cell.veg_points as usize) < min_veg_points {
            continue;
        }

        let permeability = (cell.veg_multi_returns as f64) / (cell.veg_points as f64);
        if permeability < min_permeability {
            continue;
        }

        cell.veg_heights.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let h90 = linear_percentile(&cell.veg_heights, 90.0);
        let h95 = linear_percentile(&cell.veg_heights, 95.0);

        if h90 <= 0.0 || h95 <= 0.0 {
            continue;
        }

        let total_pts = cell.total_points as f64;
        let veg_pts = cell.veg_points as f64;
        let dv = (veg_pts / total_pts) * 100.0;

        let mean = cell.veg_heights.iter().map(|&z| z as f64).sum::<f64>() / veg_pts;
        let variance = cell
            .veg_heights
            .iter()
            .map(|&z| {
                let diff = (z as f64) - mean;
                diff * diff
            })
            .sum::<f64>()
            / veg_pts;
        let std_dev = variance.sqrt();
        let relstd = std_dev / h95;

        let dp = (cell.single_returns as f64) / total_pts;

        let ln_v = -2.50 + 1.49 * h90.ln() + 0.87 * dv.ln() + 0.44 * dp - 2.44 * relstd;
        let volume_ha = ln_v.exp();

        if volume_ha.is_finite() && volume_ha > 0.0 {
            results.push(GridMetric {
                easting: cell_x as f64 + half_grid,
                northing: cell_y as f64 + half_grid,
                h90: (h90 * 100.0).round() / 100.0,
                volume_ha: (volume_ha * 100.0).round() / 100.0,
            });
        }
    }

    Ok(results)
}

fn write_csv_metrics(metrics: &[GridMetric], output_path: &Path) -> Result<()> {
    let file = File::create(output_path)
        .with_context(|| format!("Failed to create output CSV {:?}", output_path))?;
    let mut writer = BufWriter::new(file);

    writeln!(writer, "easting,northing,h90,volume_ha")?;
    for m in metrics {
        writeln!(writer, "{:.1},{:.1},{:.2},{:.2}", m.easting, m.northing, m.h90, m.volume_ha)?;
    }
    writer.flush()?;
    Ok(())
}

fn main() -> Result<()> {
    env_logger::init();
    let args = Args::parse();

    if args.threads > 0 {
        rayon::ThreadPoolBuilder::new()
            .num_threads(args.threads)
            .build_global()
            .context("Failed to configure thread pool")?;
    }

    let mut files = Vec::new();
    if args.input.is_file() {
        files.push(args.input.clone());
    } else if args.input.is_dir() {
        for entry in std::fs::read_dir(&args.input)? {
            let entry = entry?;
            let path = entry.path();
            if let Some(ext) = path.extension() {
                let ext_str = ext.to_string_lossy().to_lowercase();
                if ext_str == "laz" || ext_str == "las" {
                    files.push(path);
                }
            }
        }
        files.sort();
    } else {
        anyhow::bail!("Input path {:?} does not exist", args.input);
    }

    if files.is_empty() {
        println!("No .laz or .las files found in {:?}", args.input);
        return Ok(());
    }

    std::fs::create_dir_all(&args.output_dir)?;

    println!("=======================================================");
    println!(" Lake District Carbon Project - Fast LiDAR Processor");
    println!("=======================================================");
    println!("Found {} file(s) to process", files.len());
    println!("Grid Resolution : {} m (EPSG:27700 snapped)", args.grid_size);
    println!("HAG Mode        : {:?}", args.hag_mode);
    println!("DTM Resolution  : {} m (bilinear in-memory)", args.dtm_res);
    println!("Min Veg Height  : {} m", args.min_veg_height);
    println!("Threads         : {}", rayon::current_num_threads());
    println!("Output Dir      : {:?}", args.output_dir);
    println!("=======================================================");

    let pb = ProgressBar::new(files.len() as u64);
    pb.set_style(
        ProgressStyle::default_bar()
            .template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} tiles ({eta})")
            .unwrap()
            .progress_chars("#>-"),
    );

    let start_time = Instant::now();
    let total_valid_cells = Arc::new(AtomicUsize::new(0));

    let processed_results: Vec<(PathBuf, Result<Vec<GridMetric>>)> = files
        .par_iter()
        .map(|file_path| {
            let res = process_single_tile(
                file_path,
                args.grid_size,
                args.dtm_res,
                args.min_veg_height,
                args.min_veg_points,
                args.min_permeability,
                args.hag_mode,
            );
            pb.inc(1);
            (file_path.clone(), res)
        })
        .collect();

    pb.finish_with_message("Done!");

    let mut master_metrics = Vec::new();

    for (file_path, res) in processed_results {
        match res {
            Ok(metrics) => {
                let cell_count = metrics.len();
                total_valid_cells.fetch_add(cell_count, Ordering::Relaxed);

                let stem = file_path.file_stem().unwrap().to_string_lossy();
                let clean_stem = stem.replace("_norm", "");
                let out_csv = args.output_dir.join(format!("{}_grid.csv", clean_stem));
                if let Err(e) = write_csv_metrics(&metrics, &out_csv) {
                    eprintln!("Error saving CSV {:?}: {:?}", out_csv, e);
                }

                if args.combine_master {
                    master_metrics.extend(metrics);
                }
            }
            Err(e) => {
                eprintln!("Failed to process {:?}: {:?}", file_path, e);
            }
        }
    }

    if args.combine_master && !master_metrics.is_empty() {
        let master_file = args.output_dir.join("master_volume_grid.csv");
        println!("\nWriting combined master grid: {:?}", master_file);
        write_csv_metrics(&master_metrics, &master_file)?;
    }

    let duration = start_time.elapsed();
    let cell_total = total_valid_cells.load(Ordering::Relaxed);

    println!("\n-------------------------------------------------------");
    println!("Processing Complete!");
    println!("Total Tiles Processed : {}", files.len());
    println!("Total Woodland Cells  : {}", cell_total);
    println!("Total Time Taken      : {:.2}s ({:.2}s per tile)", duration.as_secs_f64(), duration.as_secs_f64() / (files.len() as f64));
    println!("=======================================================");

    Ok(())
}
