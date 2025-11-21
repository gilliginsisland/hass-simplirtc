#!/bin/bash
set -euo pipefail

# Script to generate Python code from LiveKit protobuf definitions in submodule

# Ensure protoc is installed
if ! command -v protoc &> /dev/null; then
    echo "protoc is not installed. Please install Protocol Buffers compiler."
    exit 1
fi

# Define paths
PROTO_DIR="protocol/protobufs"
OUTPUT_DIR="custom_components/simplirtc/protobufs"

# Generate Python code from all proto files in the directory
echo "Generating Python code from proto files in ${PROTO_DIR}/..."
echo "Processing ${PROTO_DIR}"/*.proto
protoc -I"${PROTO_DIR}" --python_out="${OUTPUT_DIR}" --pyi_out="${OUTPUT_DIR}" "${PROTO_DIR}"/*.proto
echo "Protobuf files generated successfully in $OUTPUT_DIR/"

# Modify the generated Python files to use relative imports with a leading dot
echo "Modifying import statements to use relative imports..."
sed -i '' -E 's/import livekit_([a-z]+)_pb2 as/from . import livekit_\1_pb2 as/' "${OUTPUT_DIR}"/livekit_*_pb2.py
echo "Import statements updated to relative imports with leading dot."
