## Kernel Build Script for compile and modifying Samsung a55 Kernel
This script provides a simple interface for building and patching GKI Kernels for the Samsung a55, as well as building 
dtbo, boot system_dlkm, vendor_dlkm and vendor_boot images. It also has functionality to create ak3 Zipfiles for flashing and signing
images with personal keys.

### Supports:
* Integrating KernelSU-Next
* Integrating SUSFS
* Integrate Droidspaces
* Integrating SukiSU (SUSFS not currently supported, still under development, not recommended)
* Changing sources by simply editing prebuilts.json

### Prerequisites

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y build-essential libelf-dev libssl-dev pkg-config flex bison rsync bc git wget curl
```

### Usage

```bash
python3 build_kernel.py --help
```

### Credits
* KernelSU: https://github.com/tiann/KernelSU
* KernelSU-Next: https://github.com/KernelSU-Next/KernelSU-Next
* SukiSU-Ultra: https://github.com/SukiSU-Ultra/SukiSU-Ultra
* Pershoot for providing the SUSFS patched KernelSU-Next manager: https://github.com/pershoot/KernelSU-Next/tree/dev-susfs
* Pershoot and Simonpunk for SUSFS4KSU patches: https://gitlab.com/simonpunk/susfs4ksu/ / https://gitlab.com/pershoot/susfs4ksu/
* Droidspaces-OSS: https://github.com/ravindu644/Droidspaces-OSS/
* And fiqys for providing the base Script: https://github.com/fiqys/kernel_build_script
