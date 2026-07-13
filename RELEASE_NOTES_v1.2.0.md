## 🚀 v1.2.0 - Auto Mirror & Dynamic Optimization

### 🪞 Auto Mirror Selection
- Automatically detect and select the fastest mirror site from 8 CN mirrors + 4 global CDN nodes
- Multi-dimension scoring: latency (40%), bandwidth (40%), DNS time (20%)
- SmartMirrorDownloader with TTL cache (5 min)
- CLI: `--mirror` (enable) / `--mirror-region` (cn/global/auto)

### ⚙️ Dynamic Optimization
- **OptimalShardCalculator**: Dynamic shard count based on BDP, bandwidth, file size
- **OptimalThreadCalculator**: Optimal thread count from CPU cores, latency, bandwidth
- **BandwidthProbe**: Real-time network speed and latency measurement
- **DownloadOptimizer**: Integrated optimization pipeline for HTTP downloads
- CLI: `--optimize` (default on) / `--no-optimize` / `--dry-run`

### 📊 New CLI Features
- `--dry-run`: Preview optimal parameters without downloading
- `--mirror`: Enable auto mirror selection
- `--mirror-region`: Choose mirror region (cn/global/auto)
- `--optimize` / `--no-optimize`: Toggle auto optimization

### 🔧 Architecture
- `mirror_selector.py`: New module for mirror selection
- `optimizer.py`: New module for download optimization
- `protocols/http.py`: Integrated with optimizer for dynamic shard/thread allocation
- `core.py`: New `auto_optimize` and `auto_mirror` parameters
