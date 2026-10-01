#! /bin/bash
 
PORT=22017
KEY="tmp/mykey"
 
if [ ! -f "${KEY}" ]; then
    echo "ERROR: ${KEY} not found. Run deploy_first_part.sh first." >&2
    exit 1
fi
 
ssh -i "${KEY}" -p "${PORT}" -o StrictHostKeyChecking=no "student-admin@${MACHINE}"