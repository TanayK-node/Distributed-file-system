#!/usr/bin/env python3

import os
import io
import json
import hashlib
import shutil
import threading
import time
import random
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, send_file
from werkzeug.utils import secure_filename
import uuid
from pathlib import Path
from collections import defaultdict
from enum import Enum

class OSDAState(Enum):
    UP = "up"
    DOWN = "down"
    OUT = "out"

class PGState(Enum):
    ACTIVE_CLEAN = "active+clean"
    ACTIVE_DEGRADED = "active+degraded"
    INACTIVE = "inactive"
    RECOVERING = "recovering"

class Pool:
    def __init__(self, pool_id, name, size=3, min_size=2, pg_num=64):
        self.pool_id = pool_id
        self.name = name
        self.size = size  # replication factor
        self.min_size = min_size  # minimum replicas for operation
        self.pg_num = pg_num  # number of placement groups
        self.objects = {}  # object_id -> object_metadata
       
    def get_pg_id(self, object_id):
        """Map object to placement group"""
        return hash(object_id) % self.pg_num

class PlacementGroup:
    def __init__(self, pgid, pool_id):
        self.pgid = pgid  # format: pool_id.pg_num
        self.pool_id = pool_id
        self.primary_osd = None
        self.replica_osds = []
        self.state = PGState.INACTIVE
        self.objects = set()
        self.last_scrub = None
       
    def get_acting_set(self):
        """Get all OSDs responsible for this PG"""
        if self.primary_osd is None:
            return []
        return [self.primary_osd] + self.replica_osds
   
    def is_healthy(self, osd_map):
        """Check if PG has enough healthy replicas"""
        healthy_osds = [osd for osd in self.get_acting_set()
                       if osd in osd_map and osd_map[osd].state == OSDAState.UP]
        return len(healthy_osds) >= 2  # min_size

class OSD:
    def __init__(self, osd_id, data_path, weight=1.0, rack="default"):
        self.osd_id = osd_id
        self.data_path = Path(data_path)
        self.weight = weight  # capacity weight for CRUSH
        self.rack = rack  # failure domain
        self.state = OSDAState.UP
        self.last_heartbeat = time.time()
        self.pg_assignments = set()  # PGs this OSD is responsible for
       
        # Create OSD directory structure
        self.data_path.mkdir(parents=True, exist_ok=True)
        (self.data_path / "objects").mkdir(exist_ok=True)
       
    def store_object(self, pool_id, object_id, data, metadata):
        """Store object data"""
        if self.state != OSDAState.UP:
            raise Exception(f"OSD {self.osd_id} is not up")
           
        obj_path = self.data_path / "objects" / f"{pool_id}_{object_id}"
       
        try:
            with open(obj_path, 'wb') as f:
                f.write(data)
           
            # Store metadata separately
            meta_path = obj_path.with_suffix('.meta')
            with open(meta_path, 'w') as f:
                json.dump(metadata, f)
               
            return True
        except Exception as e:
            print(f"Error storing object in OSD {self.osd_id}: {e}")
            return False
   
    def retrieve_object(self, pool_id, object_id):
        """Retrieve object data and metadata"""
        if self.state != OSDAState.UP:
            return None, None
           
        obj_path = self.data_path / "objects" / f"{pool_id}_{object_id}"
        meta_path = obj_path.with_suffix('.meta')
       
        try:
            if obj_path.exists() and meta_path.exists():
                with open(obj_path, 'rb') as f:
                    data = f.read()
                with open(meta_path, 'r') as f:
                    metadata = json.load(f)
                return data, metadata
        except Exception as e:
            print(f"Error retrieving object from OSD {self.osd_id}: {e}")
           
        return None, None
   
    def delete_object(self, pool_id, object_id):
        """Delete object"""
        obj_path = self.data_path / "objects" / f"{pool_id}_{object_id}"
        meta_path = obj_path.with_suffix('.meta')
       
        try:
            if obj_path.exists():
                os.remove(obj_path)
            if meta_path.exists():
                os.remove(meta_path)
            return True
        except Exception as e:
            print(f"Error deleting object from OSD {self.osd_id}: {e}")
            return False
   
    def heartbeat(self):
        """Update heartbeat timestamp"""
        self.last_heartbeat = time.time()
        return {
            'osd_id': self.osd_id,
            'state': self.state.value,
            'rack': self.rack,
            'weight': self.weight,
            'timestamp': self.last_heartbeat
        }

class SimpleCRUSH:
    """Simplified version of CRUSH algorithm"""
   
    def __init__(self, osds):
        self.osds = osds
        self.failure_domains = self._build_failure_map()
   
    def _build_failure_map(self):
        """Group OSDs by failure domain (rack)"""
        domains = defaultdict(list)
        for osd in self.osds.values():
            if osd.state == OSDAState.UP:
                domains[osd.rack].append(osd.osd_id)
        return domains
   
    def select_osds(self, pgid, replication_size):
        """Select OSDs for a placement group using simplified CRUSH"""
        # Use PG ID as seed for deterministic but pseudo-random selection
        random.seed(pgid)
       
        selected_osds = []
        used_racks = set()
       
        # Try to place replicas in different failure domains
        available_racks = list(self.failure_domains.keys())
        random.shuffle(available_racks)
       
        for rack in available_racks:
            if len(selected_osds) >= replication_size:
                break
               
            rack_osds = [osd_id for osd_id in self.failure_domains[rack]
                        if self.osds[osd_id].state == OSDAState.UP]
           
            if rack_osds and rack not in used_racks:
                # Select random OSD from this rack
                selected_osd = random.choice(rack_osds)
                selected_osds.append(selected_osd)
                used_racks.add(rack)
       
        # If we don't have enough racks, fill remaining slots from any available OSDs
        if len(selected_osds) < replication_size:
            all_available = [osd_id for osd_id in self.osds.keys()
                           if (self.osds[osd_id].state == OSDAState.UP and
                               osd_id not in selected_osds)]
           
            random.shuffle(all_available)
            needed = replication_size - len(selected_osds)
            selected_osds.extend(all_available[:needed])
       
        # Reset random seed
        random.seed()
       
        return selected_osds[:replication_size]

class Monitor:
    """Simplified Ceph Monitor"""
   
    def __init__(self):
        self.osd_map = {}  # osd_id -> OSD
        self.pg_map = {}   # pgid -> PlacementGroup
        self.pools = {}    # pool_id -> Pool
        self.crush = None
        self.epoch = 1
        self.last_health_check = 0
       
    def add_osd(self, osd):
        """Add OSD to cluster"""
        self.osd_map[osd.osd_id] = osd
        self.crush = SimpleCRUSH(self.osd_map)
        self._update_pg_mappings()
        self.epoch += 1
       
    def remove_osd(self, osd_id):
        """Remove OSD from cluster"""
        if osd_id in self.osd_map:
            self.osd_map[osd_id].state = OSDAState.OUT
            self._update_pg_mappings()
            self.epoch += 1
   
    def create_pool(self, name, size=3, pg_num=32):
        """Create a new storage pool"""
        pool_id = len(self.pools)
        pool = Pool(pool_id, name, size=size, pg_num=pg_num)
        self.pools[pool_id] = pool
       
        # Create placement groups for the pool
        for pg_num in range(pool.pg_num):
            pgid = f"{pool_id}.{pg_num}"
            pg = PlacementGroup(pgid, pool_id)
            self.pg_map[pgid] = pg
           
        self._update_pg_mappings()
        return pool_id
   
    def _update_pg_mappings(self):
        """Update PG to OSD mappings using CRUSH"""
        if not self.crush or not self.pools:
            return
           
        for pg in self.pg_map.values():
            pool = self.pools[pg.pool_id]
            osds = self.crush.select_osds(pg.pgid, pool.size)
           
            if osds:
                pg.primary_osd = osds[0]
                pg.replica_osds = osds[1:]
                pg.state = PGState.ACTIVE_CLEAN if len(osds) >= pool.size else PGState.ACTIVE_DEGRADED
               
                # Update OSD assignments
                for osd_id in osds:
                    if osd_id in self.osd_map:
                        self.osd_map[osd_id].pg_assignments.add(pg.pgid)
            else:
                pg.state = PGState.INACTIVE
   
    def process_heartbeat(self, osd_id):
        """Process OSD heartbeat"""
        if osd_id in self.osd_map:
            return self.osd_map[osd_id].heartbeat()
        return None
   
    def get_cluster_status(self):
        """Get overall cluster health and status"""
        total_pgs = len(self.pg_map)
        active_clean = len([pg for pg in self.pg_map.values() if pg.state == PGState.ACTIVE_CLEAN])
        degraded = len([pg for pg in self.pg_map.values() if pg.state == PGState.ACTIVE_DEGRADED])
       
        up_osds = len([osd for osd in self.osd_map.values() if osd.state == OSDAState.UP])
        total_osds = len(self.osd_map)
       
        return {
            'health': 'HEALTH_OK' if active_clean == total_pgs else 'HEALTH_WARN',
            'osds': {'up': up_osds, 'total': total_osds},
            'pgs': {'total': total_pgs, 'active_clean': active_clean, 'degraded': degraded},
            'pools': len(self.pools),
            'epoch': self.epoch
        }

class RADOS:
    """Simplified RADOS (Reliable Autonomic Distributed Object Store)"""
   
    def __init__(self, monitor):
        self.monitor = monitor
        self.lock = threading.Lock()
   
    def put_object(self, pool_name, object_id, data):
        """Store object in pool"""
        with self.lock:
            # Find pool
            pool = None
            for p in self.monitor.pools.values():
                if p.name == pool_name:
                    pool = p
                    break
           
            if not pool:
                raise Exception(f"Pool {pool_name} not found")
           
            # Get placement group
            pg_id = pool.get_pg_id(object_id)
            pgid = f"{pool.pool_id}.{pg_id}"
           
            if pgid not in self.monitor.pg_map:
                raise Exception(f"Placement group {pgid} not found")
           
            pg = self.monitor.pg_map[pgid]
           
            if pg.state == PGState.INACTIVE:
                raise Exception(f"Placement group {pgid} is inactive")
           
            # Calculate checksum
            checksum = hashlib.sha256(data).hexdigest()
           
            metadata = {
                'object_id': object_id,
                'pool_id': pool.pool_id,
                'size_bytes': len(data),
                'checksum': checksum,
                'upload_time': datetime.now().isoformat(),
                'pg_id': pgid
            }
           
            # Store in primary and replicas
            successful_stores = []
            acting_set = pg.get_acting_set()
           
            for osd_id in acting_set:
                if osd_id in self.monitor.osd_map:
                    osd = self.monitor.osd_map[osd_id]
                    try:
                        if osd.store_object(pool.pool_id, object_id, data, metadata):
                            successful_stores.append(osd_id)
                    except Exception as e:
                        print(f"Failed to store in OSD {osd_id}: {e}")
           
            if len(successful_stores) < pool.min_size:
                raise Exception(f"Failed to achieve minimum replication ({pool.min_size})")
           
            # Update pool and PG metadata
            pool.objects[object_id] = metadata
            pg.objects.add(object_id)
           
            return {
                'object_id': object_id,
                'pool': pool_name,
                'pg_id': pgid,
                'replicas': successful_stores,
                'size_bytes': len(data)
            }
   
    def get_object(self, pool_name, object_id):
        """Retrieve object from pool"""
        # Find pool
        pool = None
        for p in self.monitor.pools.values():
            if p.name == pool_name:
                pool = p
                break
       
        if not pool or object_id not in pool.objects:
            return None, None
       
        # Get placement group
        pg_id = pool.get_pg_id(object_id)
        pgid = f"{pool.pool_id}.{pg_id}"
        pg = self.monitor.pg_map[pgid]
       
        # Try to read from acting set
        acting_set = pg.get_acting_set()
       
        for osd_id in acting_set:
            if osd_id in self.monitor.osd_map:
                osd = self.monitor.osd_map[osd_id]
                try:
                    data, metadata = osd.retrieve_object(pool.pool_id, object_id)
                    if data is not None:
                        # Verify checksum
                        calculated_checksum = hashlib.sha256(data).hexdigest()
                        if calculated_checksum == metadata['checksum']:
                            return data, metadata
                        else:
                            print(f"Checksum mismatch in OSD {osd_id}")
                except Exception as e:
                    print(f"Error reading from OSD {osd_id}: {e}")
       
        return None, None
   
    def delete_object(self, pool_name, object_id):
        """Delete object from pool"""
        with self.lock:
            # Find pool
            pool = None
            for p in self.monitor.pools.values():
                if p.name == pool_name:
                    pool = p
                    break
           
            if not pool or object_id not in pool.objects:
                return False
           
            # Get placement group
            pg_id = pool.get_pg_id(object_id)
            pgid = f"{pool.pool_id}.{pg_id}"
            pg = self.monitor.pg_map[pgid]
           
            # Delete from all OSDs in acting set
            acting_set = pg.get_acting_set()
           
            for osd_id in acting_set:
                if osd_id in self.monitor.osd_map:
                    osd = self.monitor.osd_map[osd_id]
                    osd.delete_object(pool.pool_id, object_id)
           
            # Remove from metadata
            del pool.objects[object_id]
            pg.objects.discard(object_id)
           
            return True
   
    def list_objects(self, pool_name):
        """List all objects in pool"""
        pool = None
        for p in self.monitor.pools.values():
            if p.name == pool_name:
                pool = p
                break
       
        if not pool:
            return []
       
        objects = []
        for object_id, metadata in pool.objects.items():
            # Get PG health
            pg_id = pool.get_pg_id(object_id)
            pgid = f"{pool.pool_id}.{pg_id}"
            pg = self.monitor.pg_map[pgid]
           
            healthy_replicas = len([osd for osd in pg.get_acting_set()
                                  if osd in self.monitor.osd_map and
                                     self.monitor.osd_map[osd].state == OSDAState.UP])
           
            objects.append({
                'object_id': object_id,
                'size_bytes': metadata['size_bytes'],
                'upload_time': metadata['upload_time'],
                'pg_id': pgid,
                'healthy_replicas': healthy_replicas,
                'total_replicas': len(pg.get_acting_set())
            })
       
        return objects

class SimpleCephCluster:
    """Main cluster management class"""
   
    def __init__(self):
        self.monitor = Monitor()
        self.rados = RADOS(self.monitor)
       
        # Initialize cluster
        self._initialize_cluster()
   
    def _initialize_cluster(self):
        """Initialize a simple cluster"""
        # Create OSDs in different racks for failure domain separation
        racks = ['rack1', 'rack1', 'rack2', 'rack2', 'rack3']
        base_path = Path("./ceph_data")
        base_path.mkdir(exist_ok=True)
       
        for i in range(5):
            osd_path = base_path / f"osd.{i}"
            osd = OSD(i, osd_path, weight=1.0, rack=racks[i])
            self.monitor.add_osd(osd)
       
        # Create default pools
        self.monitor.create_pool("default", size=3, pg_num=32)
        self.monitor.create_pool("metadata", size=3, pg_num=16)
   
    def create_pool(self, name, size=3, pg_num=32):
        """Create a new storage pool"""
        return self.monitor.create_pool(name, size, pg_num)
   
    def put_object(self, pool_name, object_name, data):
        """Store object in cluster"""
        return self.rados.put_object(pool_name, object_name, data)
   
    def get_object(self, pool_name, object_name):
        """Retrieve object from cluster"""
        return self.rados.get_object(pool_name, object_name)
   
    def delete_object(self, pool_name, object_name):
        """Delete object from cluster"""
        return self.rados.delete_object(pool_name, object_name)
   
    def list_objects(self, pool_name="default"):
        """List objects in pool"""
        return self.rados.list_objects(pool_name)
   
    def get_cluster_status(self):
        """Get cluster status"""
        return self.monitor.get_cluster_status()
   
    def get_detailed_status(self):
        """Get detailed cluster information"""
        status = self.monitor.get_cluster_status()
       
        # Add pool information
        pool_info = []
        for pool in self.monitor.pools.values():
            pool_info.append({
                'id': pool.pool_id,
                'name': pool.name,
                'size': pool.size,
                'min_size': pool.min_size,
                'pg_num': pool.pg_num,
                'objects': len(pool.objects)
            })
       
        # Add OSD information
        osd_info = []
        for osd in self.monitor.osd_map.values():
            osd_info.append({
                'id': osd.osd_id,
                'state': osd.state.value,
                'rack': osd.rack,
                'weight': osd.weight,
                'pgs': len(osd.pg_assignments)
            })
       
        # Add PG information
        pg_states = defaultdict(int)
        for pg in self.monitor.pg_map.values():
            pg_states[pg.state.value] += 1
       
        return {
            **status,
            'pools': pool_info,
            'osds_detailed': osd_info,
            'pg_states': dict(pg_states)
        }
   
    def set_osd_state(self, osd_id, state):
        """Set OSD state (up/down)"""
        if osd_id in self.monitor.osd_map:
            if state == "up":
                self.monitor.osd_map[osd_id].state = OSDAState.UP
            elif state == "down":
                self.monitor.osd_map[osd_id].state = OSDAState.DOWN
            elif state == "out":
                self.monitor.osd_map[osd_id].state = OSDAState.OUT
           
            self.monitor._update_pg_mappings()
            return True
        return False

# Flask Web Application
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

# Initialize the Ceph-like cluster
cluster = SimpleCephCluster()

# HTML Template (Enhanced for Ceph-like features)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Simple Ceph-like Distributed Storage</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .section {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 4px solid #007bff;
        }
        .health-ok { border-left-color: #28a745; }
        .health-warn { border-left-color: #ffc107; }
        .health-error { border-left-color: #dc3545; }
       
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
        .state-up { color: #28a745; font-weight: bold; }
        .state-down { color: #dc3545; font-weight: bold; }
        .state-out { color: #6c757d; font-weight: bold; }
       
        .btn {
            padding: 8px 16px;
            margin: 4px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        .btn-primary { background: #007bff; color: white; }
        .btn-success { background: #28a745; color: white; }
        .btn-warning { background: #ffc107; color: #212529; }
        .btn-danger { background: #dc3545; color: white; }
        .btn-secondary { background: #6c757d; color: white; }
       
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
        .tabs {
            display: flex;
            border-bottom: 1px solid #ddd;
            margin-bottom: 20px;
        }
        .tab {
            padding: 10px 20px;
            cursor: pointer;
            border-bottom: 2px solid transparent;
        }
        .tab.active {
            border-bottom-color: #007bff;
            background-color: #e3f2fd;
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Simple Ceph-like Distributed Storage System</h1>
       
        <div class="section health-ok" id="healthSection">
            <h3>Cluster Health</h3>
            <div id="clusterHealth"></div>
        </div>
       
        <div class="tabs">
            <div class="tab active" onclick="showTab('objects')">Objects</div>
            <div class="tab" onclick="showTab('pools')">Pools</div>
            <div class="tab" onclick="showTab('osds')">OSDs</div>
            <div class="tab" onclick="showTab('pgs')">Placement Groups</div>
        </div>
       
        <div id="objects" class="tab-content active">
            <div class="section">
                <h3>Upload Object</h3>
                <form id="uploadForm" enctype="multipart/form-data">
                    <input type="file" id="fileInput" name="file" required>
                    <select id="poolSelect">
                        <option value="default">default</option>
                        <option value="metadata">metadata</option>
                    </select>
                    <button type="submit" class="btn btn-primary">Upload to Pool</button>
                </form>
                <div id="uploadStatus"></div>
            </div>
           
            <div class="section">
                <h3>Objects</h3>
                <button onclick="loadObjects()" class="btn btn-secondary">Refresh Objects</button>
                <table id="objectsTable">
                    <thead>
                        <tr>
                            <th>Object ID</th>
                            <th>Size</th>
                            <th>Upload Time</th>
                            <th>PG ID</th>
                            <th>Replicas</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="objectsBody"></tbody>
                </table>
            </div>
        </div>
       
        <div id="pools" class="tab-content">
            <div class="section">
                <h3>Storage Pools</h3>
                <table id="poolsTable">
                    <thead>
                        <tr>
                            <th>Pool ID</th>
                            <th>Name</th>
                            <th>Size</th>
                            <th>Min Size</th>
                            <th>PG Num</th>
                            <th>Objects</th>
                        </tr>
                    </thead>
                    <tbody id="poolsBody"></tbody>
                </table>
            </div>
        </div>
       
        <div id="osds" class="tab-content">
            <div class="section">
                <h3>Object Storage Daemons (OSDs)</h3>
                <table id="osdsTable">
                    <thead>
                        <tr>
                            <th>OSD ID</th>
                            <th>State</th>
                            <th>Rack</th>
                            <th>Weight</th>
                            <th>PGs</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="osdsBody"></tbody>
                </table>
            </div>
        </div>
       
        <div id="pgs" class="tab-content">
            <div class="section">
                <h3>Placement Groups</h3>
                <div id="pgStates"></div>
            </div>
        </div>
    </div>

    <script>
        function showTab(tabName) {
            // Hide all tab contents
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });
           
            // Remove active class from all tabs
            document.querySelectorAll('.tab').forEach(tab => {
                tab.classList.remove('active');
            });
           
            // Show selected tab content
            document.getElementById(tabName).classList.add('active');
           
            // Add active class to clicked tab
            event.target.classList.add('active');
           
            // Load data for the tab
            if (tabName === 'objects') loadObjects();
            else if (tabName === 'pools') loadPools();
            else if (tabName === 'osds') loadOSDs();
            else if (tabName === 'pgs') loadPGs();
        }
       
        // Upload object
        document.getElementById('uploadForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData();
            const fileInput = document.getElementById('fileInput');
            const poolSelect = document.getElementById('poolSelect');
           
            if (!fileInput.files[0]) {
                showStatus('Please select a file', 'error');
                return;
            }
           
            formData.append('file', fileInput.files[0]);
            formData.append('pool', poolSelect.value);
           
            try {
                showStatus('Uploading...', 'success');
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });
               
                const result = await response.json();
               
                if (response.ok) {
                    showStatus(`Object stored successfully! PG: ${result.pg_id}, Replicas: ${result.replicas.length}`, 'success');
                    fileInput.value = '';
                    loadObjects();
                    loadClusterHealth();
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
       
        // Load cluster health
        async function loadClusterHealth() {
            try {
                const response = await fetch('/status');
                const status = await response.json();
               
                const healthSection = document.getElementById('healthSection');
                const healthDiv = document.getElementById('clusterHealth');
               
                // Update health section color
                healthSection.className = `section ${status.health === 'HEALTH_OK' ? 'health-ok' : 'health-warn'}`;
               
                healthDiv.innerHTML = `
                    <p><strong>Health:</strong> ${status.health}</p>
                    <p><strong>OSDs:</strong> ${status.osds.up}/${status.osds.total} up</p>
                    <p><strong>PGs:</strong> ${status.pgs.active_clean} active+clean, ${status.pgs.degraded} degraded</p>
                    <p><strong>Pools:</strong> ${status.pools}</p>
                    <p><strong>Epoch:</strong> ${status.epoch}</p>
                `;
            } catch (error) {
                console.error('Error loading cluster health:', error);
            }
        }
       
        // Load objects
        async function loadObjects() {
            try {
                const poolSelect = document.getElementById('poolSelect');
                const pool = poolSelect.value || 'default';
               
                const response = await fetch(`/objects/${pool}`);
                const objects = await response.json();
               
                const tbody = document.getElementById('objectsBody');
                tbody.innerHTML = '';
               
                objects.forEach(obj => {
                    const row = tbody.insertRow();
                    row.innerHTML = `
                        <td>${obj.object_id}</td>
                        <td>${(obj.size_bytes / 1024).toFixed(2)} KB</td>
                        <td>${new Date(obj.upload_time).toLocaleString()}</td>
                        <td>${obj.pg_id}</td>
                        <td>${obj.healthy_replicas}/${obj.total_replicas}</td>
                        <td>
                            <button onclick="downloadObject('${pool}', '${obj.object_id}')" class="btn btn-primary">Download</button>
                            <button onclick="deleteObject('${pool}', '${obj.object_id}')" class="btn btn-danger">Delete</button>
                        </td>
                    `;
                });
            } catch (error) {
                console.error('Error loading objects:', error);
            }
        }
       
        // Load pools
        async function loadPools() {
            try {
                const response = await fetch('/detailed_status');
                const status = await response.json();
               
                const tbody = document.getElementById('poolsBody');
                tbody.innerHTML = '';
               
                status.pools.forEach(pool => {
                    const row = tbody.insertRow();
                    row.innerHTML = `
                        <td>${pool.id}</td>
                        <td>${pool.name}</td>
                        <td>${pool.size}</td>
                        <td>${pool.min_size}</td>
                        <td>${pool.pg_num}</td>
                        <td>${pool.objects}</td>
                    `;
                });
            } catch (error) {
                console.error('Error loading pools:', error);
            }
        }
       
        // Load OSDs
        async function loadOSDs() {
            try {
                const response = await fetch('/detailed_status');
                const status = await response.json();
               
                const tbody = document.getElementById('osdsBody');
                tbody.innerHTML = '';
               
                status.osds_detailed.forEach(osd => {
                    const row = tbody.insertRow();
                    const stateClass = `state-${osd.state}`;
                    const actionText = osd.state === 'up' ? 'Set Down' : 'Set Up';
                    const actionState = osd.state === 'up' ? 'down' : 'up';
                   
                    row.innerHTML = `
                        <td>osd.${osd.id}</td>
                        <td class="${stateClass}">${osd.state.toUpperCase()}</td>
                        <td>${osd.rack}</td>
                        <td>${osd.weight}</td>
                        <td>${osd.pgs}</td>
                        <td>
                            <button onclick="setOSDState(${osd.id}, '${actionState}')" class="btn btn-warning">${actionText}</button>
                            <button onclick="setOSDState(${osd.id}, 'out')" class="btn btn-secondary">Set Out</button>
                        </td>
                    `;
                });
            } catch (error) {
                console.error('Error loading OSDs:', error);
            }
        }
       
        // Load PGs
        async function loadPGs() {
            try {
                const response = await fetch('/detailed_status');
                const status = await response.json();
               
                const pgDiv = document.getElementById('pgStates');
                let html = '<h4>Placement Group States:</h4><ul>';
               
                for (const [state, count] of Object.entries(status.pg_states)) {
                    html += `<li><strong>${state}:</strong> ${count}</li>`;
                }
                html += '</ul>';
               
                pgDiv.innerHTML = html;
            } catch (error) {
                console.error('Error loading PGs:', error);
            }
        }
       
        // Download object
        async function downloadObject(pool, objectId) {
            try {
                const response = await fetch(`/download/${pool}/${objectId}`);
               
                if (response.ok) {
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = objectId;
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
       
        // Delete object
        async function deleteObject(pool, objectId) {
            if (!confirm(`Are you sure you want to delete object ${objectId}?`)) {
                return;
            }
           
            try {
                const response = await fetch(`/delete/${pool}/${objectId}`, {
                    method: 'DELETE'
                });
               
                if (response.ok) {
                    loadObjects();
                    loadClusterHealth();
                } else {
                    const error = await response.json();
                    alert(`Delete failed: ${error.error}`);
                }
            } catch (error) {
                alert(`Delete failed: ${error.message}`);
            }
        }
       
        // Set OSD state
        async function setOSDState(osdId, state) {
            try {
                const response = await fetch(`/osd/${osdId}/${state}`, {
                    method: 'POST'
                });
               
                if (response.ok) {
                    loadOSDs();
                    loadClusterHealth();
                } else {
                    const error = await response.json();
                    alert(`OSD operation failed: ${error.error}`);
                }
            } catch (error) {
                alert(`OSD operation failed: ${error.message}`);
            }
        }
       
        // Load initial data
        window.addEventListener('load', () => {
            loadClusterHealth();
            loadObjects();
           
            // Refresh cluster health every 10 seconds
            setInterval(loadClusterHealth, 10000);
        });
    </script>
</body>
</html>
"""

# Flask Routes

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
       
        pool_name = request.form.get('pool', 'default')
        filename = secure_filename(file.filename)
        file_data = file.read()
       
        result = cluster.put_object(pool_name, filename, file_data)
        return jsonify(result)
       
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download/<pool_name>/<object_id>')
def download_file(pool_name, object_id):
    try:
        file_data, metadata = cluster.get_object(pool_name, object_id)
       
        if file_data is None:
            return jsonify({'error': 'Object not found or not available'}), 404
       
        file_buffer = io.BytesIO(file_data)
        file_buffer.seek(0)
       
        return send_file(
            file_buffer,
            as_attachment=True,
            download_name=object_id,
            mimetype='application/octet-stream'
        )
       
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete/<pool_name>/<object_id>', methods=['DELETE'])
def delete_file(pool_name, object_id):
    try:
        success = cluster.delete_object(pool_name, object_id)
        if success:
            return jsonify({'message': 'Object deleted successfully'})
        else:
            return jsonify({'error': 'Object not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/objects/<pool_name>')
def list_objects(pool_name):
    try:
        return jsonify(cluster.list_objects(pool_name))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def cluster_status():
    return jsonify(cluster.get_cluster_status())

@app.route('/detailed_status')
def detailed_status():
    return jsonify(cluster.get_detailed_status())

@app.route('/osd/<int:osd_id>/<state>', methods=['POST'])
def set_osd_state(osd_id, state):
    try:
        success = cluster.set_osd_state(osd_id, state)
        if success:
            return jsonify({'message': f'OSD {osd_id} state set to {state}'})
        else:
            return jsonify({'error': 'OSD not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/create_pool', methods=['POST'])
def create_pool():
    try:
        data = request.get_json()
        name = data.get('name')
        size = data.get('size', 3)
        pg_num = data.get('pg_num', 32)
       
        pool_id = cluster.create_pool(name, size, pg_num)
        return jsonify({'pool_id': pool_id, 'message': f'Pool {name} created successfully'})
       
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("Starting Simple Ceph-like Distributed Storage System...")
    print("Web interface available at: http://localhost:5000")
    print("\nCeph-inspired Features:")
    print("- RADOS object storage with pools")
    print("- Placement groups for scalable data distribution")
    print("- Simplified CRUSH algorithm with rack awareness")
    print("- Monitor service for cluster state management")
    print("- OSD management (up/down/out states)")
    print("- Multi-pool object storage")
    print("- Failure domain separation")
    print("- Cluster health monitoring")
   
    app.run(debug=True, host='0.0.0.0', port=5000)
