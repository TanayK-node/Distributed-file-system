#!/usr/bin/env python3

import os
import io
import json
import hashlib
import shutil
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, send_file
from werkzeug.utils import secure_filename
import uuid
from pathlib import Path

class StorageNode:
    def __init__(self, node_id, base_path, capacity_mb=1000):
        self.node_id = node_id
        self.base_path = Path(base_path)
        self.capacity_mb = capacity_mb
        self.is_online = True
        self.files = {}  # file_id -> file_metadata
        
        # Create node directory if it doesn't exist
        self.base_path.mkdir(parents=True, exist_ok=True)
        
    def store_file(self, file_id, file_data, metadata):
        """Store a file in this node"""
        if not self.is_online:
            raise Exception(f"Node {self.node_id} is offline")
            
        file_path = self.base_path / file_id
        
        try:
            with open(file_path, 'wb') as f:
                f.write(file_data)
            
            self.files[file_id] = {
                **metadata,
                'stored_at': datetime.now().isoformat(),
                'file_path': str(file_path)
            }
            return True
        except Exception as e:
            print(f"Error storing file in node {self.node_id}: {e}")
            return False
    
    def retrieve_file(self, file_id):
        """Retrieve a file from this node"""
        if not self.is_online or file_id not in self.files:
            return None
            
        file_path = Path(self.files[file_id]['file_path'])
        if file_path.exists():
            with open(file_path, 'rb') as f:
                return f.read()
        return None
    
    def delete_file(self, file_id):
        """Delete a file from this node"""
        if file_id in self.files:
            file_path = Path(self.files[file_id]['file_path'])
            if file_path.exists():
                os.remove(file_path)
            del self.files[file_id]
            return True
        return False
    
    def get_used_space(self):
        """Calculate used space in MB"""
        total_size = 0
        for file_info in self.files.values():
            file_path = Path(file_info['file_path'])
            if file_path.exists():
                total_size += file_path.stat().st_size
        return total_size / (1024 * 1024)  # Convert to MB
    
    def health_check(self):
        """Return node health information"""
        return {
            'node_id': self.node_id,
            'is_online': self.is_online,
            'files_count': len(self.files),
            'used_space_mb': round(self.get_used_space(), 2),
            'capacity_mb': self.capacity_mb,
            'utilization': round((self.get_used_space() / self.capacity_mb) * 100, 2)
        }

class DistributedFileSystem:
    def __init__(self, replication_factor=3):
        self.nodes = {}
        self.file_metadata = {}  # file_id -> global metadata
        self.replication_factor = replication_factor
        self.lock = threading.Lock()
        
        # Initialize storage nodes
        self.initialize_nodes()
        
    def initialize_nodes(self):
        """Initialize storage nodes"""
        base_storage_path = Path("./storage")
        base_storage_path.mkdir(exist_ok=True)
        
        for i in range(5):  # Create 5 storage nodes
            node_id = f"node_{i+1}"
            node_path = base_storage_path / node_id
            self.nodes[node_id] = StorageNode(node_id, node_path)
    
    def hash_filename(self, filename):
        """Generate hash for filename to determine primary node"""
        return int(hashlib.md5(filename.encode()).hexdigest(), 16)
    
    def select_nodes(self, filename):
        """Select nodes for storing file replicas"""
        online_nodes = [node for node in self.nodes.values() if node.is_online]
        
        if len(online_nodes) == 0:
            return []
        
        # Select primary node based on hash
        primary_index = self.hash_filename(filename) % len(online_nodes)
        selected_nodes = [online_nodes[primary_index]]
        
        # Add replica nodes
        num_replicas = min(self.replication_factor, len(online_nodes))
        for i in range(1, num_replicas):
            replica_index = (primary_index + i) % len(online_nodes)
            selected_nodes.append(online_nodes[replica_index])
        
        return selected_nodes
    
    def store_file(self, filename, file_data):
        """Store file across multiple nodes"""
        with self.lock:
            file_id = str(uuid.uuid4())
            selected_nodes = self.select_nodes(filename)
            
            if not selected_nodes:
                raise Exception("No online nodes available")
            
            # Calculate checksum
            checksum = hashlib.sha256(file_data).hexdigest()
            
            metadata = {
                'file_id': file_id,
                'original_filename': filename,
                'size_bytes': len(file_data),
                'checksum': checksum,
                'upload_time': datetime.now().isoformat(),
                'replicas': []
            }
            
            successful_stores = []
            
            # Store file in selected nodes
            for node in selected_nodes:
                try:
                    if node.store_file(file_id, file_data, metadata):
                        successful_stores.append(node.node_id)
                        metadata['replicas'].append(node.node_id)
                except Exception as e:
                    print(f"Failed to store in node {node.node_id}: {e}")
            
            if successful_stores:
                self.file_metadata[file_id] = metadata
                return {
                    'file_id': file_id,
                    'filename': filename,
                    'size_bytes': len(file_data),
                    'replicas': successful_stores,
                    'replication_factor': len(successful_stores)
                }
            else:
                raise Exception("Failed to store file in any node")
    
    def retrieve_file(self, file_id):
        """Retrieve file from available replicas"""
        if file_id not in self.file_metadata:
            return None, None
        
        metadata = self.file_metadata[file_id]
        
        # Try to retrieve from any available replica
        for node_id in metadata['replicas']:
            node = self.nodes[node_id]
            if node.is_online:
                file_data = node.retrieve_file(file_id)
                if file_data:
                    # Verify checksum
                    calculated_checksum = hashlib.sha256(file_data).hexdigest()
                    if calculated_checksum == metadata['checksum']:
                        return file_data, metadata
                    else:
                        print(f"Checksum mismatch for file {file_id} in node {node_id}")
        
        return None, None
    
    def delete_file(self, file_id):
        """Delete file from all replicas"""
        if file_id not in self.file_metadata:
            return False
        
        with self.lock:
            metadata = self.file_metadata[file_id]
            
            # Delete from all replica nodes
            for node_id in metadata['replicas']:
                node = self.nodes[node_id]
                node.delete_file(file_id)
            
            # Remove from global metadata
            del self.file_metadata[file_id]
            return True
    
    def list_files(self):
        """List all files in the system"""
        return [
            {
                'file_id': file_id,
                'filename': metadata['original_filename'],
                'size_bytes': metadata['size_bytes'],
                'upload_time': metadata['upload_time'],
                'replicas': metadata['replicas'],
                'available_replicas': len([
                    node_id for node_id in metadata['replicas'] 
                    if self.nodes[node_id].is_online
                ])
            }
            for file_id, metadata in self.file_metadata.items()
        ]
    
    def get_system_status(self):
        """Get overall system status"""
        return {
            'nodes': [node.health_check() for node in self.nodes.values()],
            'total_files': len(self.file_metadata),
            'replication_factor': self.replication_factor
        }
    
    def toggle_node_status(self, node_id):
        """Toggle node online/offline status"""
        if node_id in self.nodes:
            self.nodes[node_id].is_online = not self.nodes[node_id].is_online
            return True
        return False

# Flask Web Application
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

# Initialize the distributed file system
dfs = DistributedFileSystem()

# HTML Template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Distributed File System</title>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            margin: 20px; 
            background-color: #f5f5f5;
        }
        .container { 
            max-width: 1200px; 
            margin: 0 auto; 
            background: white; 
            padding: 20px; 
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .upload-section { 
            background: #e3f2fd; 
            padding: 20px; 
            border-radius: 8px; 
            margin-bottom: 20px;
        }
        .files-section, .nodes-section { 
            margin-top: 20px; 
        }
        table { 
            width: 100%; 
            border-collapse: collapse; 
            margin-top: 10px;
        }
        th, td { 
            border: 1px solid #ddd; 
            padding: 8px; 
            text-align: left; 
        }
        th { 
            background-color: #f2f2f2; 
        }
        .node-online { 
            color: green; 
            font-weight: bold; 
        }
        .node-offline { 
            color: red; 
            font-weight: bold; 
        }
        .btn { 
            padding: 8px 16px; 
            margin: 4px; 
            border: none; 
            border-radius: 4px; 
            cursor: pointer;
        }
        .btn-primary { 
            background: #007bff; 
            color: white; 
        }
        .btn-danger { 
            background: #dc3545; 
            color: white; 
        }
        .btn-secondary { 
            background: #6c757d; 
            color: white; 
        }
        .status-message { 
            padding: 10px; 
            margin: 10px 0; 
            border-radius: 4px; 
        }
        .success { 
            background: #d4edda; 
            border: 1px solid #c3e6cb; 
            color: #155724; 
        }
        .error { 
            background: #f8d7da; 
            border: 1px solid #f5c6cb; 
            color: #721c24; 
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Distributed File System</h1>
        
        <div class="upload-section">
            <h3>Upload File</h3>
            <form id="uploadForm" enctype="multipart/form-data">
                <input type="file" id="fileInput" name="file" required>
                <button type="submit" class="btn btn-primary">Upload</button>
            </form>
            <div id="uploadStatus"></div>
        </div>
        
        <div class="files-section">
            <h3>Files</h3>
            <button onclick="loadFiles()" class="btn btn-secondary">Refresh Files</button>
            <table id="filesTable">
                <thead>
                    <tr>
                        <th>Filename</th>
                        <th>Size</th>
                        <th>Upload Time</th>
                        <th>Replicas</th>
                        <th>Available</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="filesBody"></tbody>
            </table>
        </div>
        
        <div class="nodes-section">
            <h3>Storage Nodes</h3>
            <button onclick="loadNodes()" class="btn btn-secondary">Refresh Nodes</button>
            <table id="nodesTable">
                <thead>
                    <tr>
                        <th>Node ID</th>
                        <th>Status</th>
                        <th>Files Count</th>
                        <th>Used Space (MB)</th>
                        <th>Utilization</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="nodesBody"></tbody>
            </table>
        </div>
    </div>

    <script>
        // Upload file
        document.getElementById('uploadForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData();
            const fileInput = document.getElementById('fileInput');
            
            if (!fileInput.files[0]) {
                showStatus('Please select a file', 'error');
                return;
            }
            
            formData.append('file', fileInput.files[0]);
            
            try {
                showStatus('Uploading...', 'success');
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    showStatus(`File uploaded successfully! Replicated to ${result.replication_factor} nodes.`, 'success');
                    fileInput.value = '';
                    loadFiles();
                } else {
                    showStatus(`Upload failed: ${result.error}`, 'error');
                }
            } catch (error) {
                showStatus(`Upload failed: ${error.message}`, 'error');
            }
        });
        
        // Show status message
        function showStatus(message, type) {
            const statusDiv = document.getElementById('uploadStatus');
            statusDiv.innerHTML = `<div class="status-message ${type}">${message}</div>`;
            setTimeout(() => {
                statusDiv.innerHTML = '';
            }, 5000);
        }
        
        // Load files
        async function loadFiles() {
            try {
                const response = await fetch('/files');
                const files = await response.json();
                
                const tbody = document.getElementById('filesBody');
                tbody.innerHTML = '';
                
                files.forEach(file => {
                    const row = tbody.insertRow();
                    row.innerHTML = `
                        <td>${file.filename}</td>
                        <td>${(file.size_bytes / 1024).toFixed(2)} KB</td>
                        <td>${new Date(file.upload_time).toLocaleString()}</td>
                        <td>${file.replicas.join(', ')}</td>
                        <td>${file.available_replicas}/${file.replicas.length}</td>
                        <td>
                            <button onclick="downloadFile('${file.file_id}', '${file.filename}')" class="btn btn-primary">Download</button>
                            <button onclick="deleteFile('${file.file_id}')" class="btn btn-danger">Delete</button>
                        </td>
                    `;
                });
            } catch (error) {
                console.error('Error loading files:', error);
            }
        }
        
        // Load nodes
        async function loadNodes() {
            try {
                const response = await fetch('/status');
                const status = await response.json();
                
                const tbody = document.getElementById('nodesBody');
                tbody.innerHTML = '';
                
                status.nodes.forEach(node => {
                    const row = tbody.insertRow();
                    const statusClass = node.is_online ? 'node-online' : 'node-offline';
                    const statusText = node.is_online ? 'Online' : 'Offline';
                    const toggleText = node.is_online ? 'Take Offline' : 'Bring Online';
                    
                    row.innerHTML = `
                        <td>${node.node_id}</td>
                        <td class="${statusClass}">${statusText}</td>
                        <td>${node.files_count}</td>
                        <td>${node.used_space_mb}</td>
                        <td>${node.utilization}%</td>
                        <td>
                            <button onclick="toggleNode('${node.node_id}')" class="btn btn-secondary">${toggleText}</button>
                        </td>
                    `;
                });
            } catch (error) {
                console.error('Error loading nodes:', error);
            }
        }
        
        // Download file
        async function downloadFile(fileId, filename) {
            try {
                const response = await fetch(`/download/${fileId}`);
                
                if (response.ok) {
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                } else {
                    const error = await response.json();
                    alert(`Download failed: ${error.error}`);
                }
            } catch (error) {
                alert(`Download failed: ${error.message}`);
            }
        }
        
        // Delete file
        async function deleteFile(fileId) {
            if (!confirm('Are you sure you want to delete this file?')) {
                return;
            }
            
            try {
                const response = await fetch(`/delete/${fileId}`, {
                    method: 'DELETE'
                });
                
                if (response.ok) {
                    loadFiles();
                } else {
                    const error = await response.json();
                    alert(`Delete failed: ${error.error}`);
                }
            } catch (error) {
                alert(`Delete failed: ${error.message}`);
            }
        }
        
        // Toggle node status
        async function toggleNode(nodeId) {
            try {
                const response = await fetch(`/node/${nodeId}/toggle`, {
                    method: 'POST'
                });
                
                if (response.ok) {
                    loadNodes();
                } else {
                    const error = await response.json();
                    alert(`Toggle failed: ${error.error}`);
                }
            } catch (error) {
                alert(`Toggle failed: ${error.message}`);
            }
        }
        
        // Load initial data
        window.addEventListener('load', () => {
            loadFiles();
            loadNodes();
        });
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        filename = secure_filename(file.filename)
        file_data = file.read()
        
        result = dfs.store_file(filename, file_data)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download/<file_id>')
def download_file(file_id):
    try:
        file_data, metadata = dfs.retrieve_file(file_id)
        
        if file_data is None:
            return jsonify({'error': 'File not found or not available'}), 404
        
        # Use BytesIO to serve file directly from memory
        file_buffer = io.BytesIO(file_data)
        file_buffer.seek(0)
        
        return send_file(
            file_buffer, 
            as_attachment=True, 
            download_name=metadata['original_filename'],
            mimetype='application/octet-stream'
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete/<file_id>', methods=['DELETE'])
def delete_file(file_id):
    try:
        success = dfs.delete_file(file_id)
        if success:
            return jsonify({'message': 'File deleted successfully'})
        else:
            return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/files')
def list_files():
    return jsonify(dfs.list_files())

@app.route('/status')
def system_status():
    return jsonify(dfs.get_system_status())

@app.route('/node/<node_id>/toggle', methods=['POST'])
def toggle_node(node_id):
    try:
        success = dfs.toggle_node_status(node_id)
        if success:
            return jsonify({'message': f'Node {node_id} status toggled'})
        else:
            return jsonify({'error': 'Node not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("Starting Distributed File System...")
    print("Web interface available at: http://localhost:5000")
    print("\nFeatures:")
    print("- File upload with automatic replication")
    print("- File download with failover")
    print("- Node management (online/offline)")
    print("- System status monitoring")
    print("- Checksum verification")
    
    app.run(debug=True, host='0.0.0.0', port=5000)