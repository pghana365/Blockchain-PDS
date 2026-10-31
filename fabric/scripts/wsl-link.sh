#!/usr/bin/env bash
# Creates ~/pds -> the Windows project folder (avoids the space in the path)
# and reports whether chmod works on the Windows drive.
set -u
SRC="/mnt/c/Users/pghan/Documents/Github/Blockchain PDS"
ln -sfn "$SRC" "$HOME/pds"
ls -ld "$HOME/pds"
cd "$HOME/pds" || exit 1
touch .permtest && chmod 755 .permtest
ls -l .permtest
rm -f .permtest
grep -E " /mnt/c " /proc/mounts | cut -c1-200
