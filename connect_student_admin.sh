#! /bin/bash
 
PORT=22017
MACHINE=paffenroth-23.dyn.wpi.edu
# Adjust if your key is in a subfolder of the Desktop
KEY=$HOME/Desktop/student-admin_key
if [ ! -f "${KEY}" ]; then
    echo "ERROR: default key not found at ${KEY}" >&2
    exit 1
fi
 
ssh -i "${KEY}" -p "${PORT}" -o StrictHostKeyChecking=no "student-admin@${MACHINE}"