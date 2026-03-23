"""
fix_torchvision.py — reinstall torchvision to match current torch version
Handles both CPU and CUDA builds automatically.
torch 2.x → torchvision 0.(x+15).z  (e.g. torch 2.5.1 → torchvision 0.20.1)
"""
import subprocess, sys, re

try:
    import torch
    tv = torch.__version__
    match = re.match(r'(\d+)\.(\d+)\.(\d+)', tv)
    if not match:
        print(f"Could not parse torch version: {tv}")
        sys.exit(1)

    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    tv_version = f"0.{minor + 15}.{patch}"   # e.g. 0.20.1

    cuda_ver = getattr(torch.version, 'cuda', None)

    if cuda_ver:
        cu = 'cu' + cuda_ver.replace('.', '')          # e.g. cu124
        idx = f'https://download.pytorch.org/whl/{cu}'
        print(f"[torchvision fix] torch {tv} + CUDA {cuda_ver} → installing torchvision {tv_version}+{cu}")
        subprocess.run([sys.executable, '-m', 'pip', 'install',
                        f'torchvision=={tv_version}',
                        '--index-url', idx,
                        '--force-reinstall', '--no-deps'], check=True)
    else:
        print(f"[torchvision fix] torch {tv} (CPU) → installing torchvision {tv_version}")
        subprocess.run([sys.executable, '-m', 'pip', 'install',
                        f'torchvision=={tv_version}',
                        '--force-reinstall', '--no-deps'], check=True)

    print("✓ torchvision reinstalled — version now matches torch")

except ImportError:
    print("torch not found in this environment")
    sys.exit(1)
