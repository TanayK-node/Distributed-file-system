# Ceph-Like Distributed Object Storage System

A lightweight, Python-based simulation of a **Ceph-style distributed object storage cluster** with a Flask web interface for real-time monitoring and file operations.

---

## 🚀 Features
- **Object-Based Storage** – Files are stored as objects with metadata and SHA256 checksums.
- **Replication & Fault Tolerance** – Each object is replicated across multiple OSDs.
- **Dynamic Rebalancing** – CRUSH-like algorithm automatically reassigns data when OSDs fail.
- **Rack Awareness** – Replicas are distributed across racks for higher availability.
- **Web Dashboard** – Upload, download, and delete objects while monitoring cluster health.
- **Integrity Verification** – Checksum validation on every read operation.

---

## 🏗 Architecture

| Component | Role |
|-----------|------|
| **OSD (Object Storage Daemon)** | Stores objects on disk with metadata and checksum. Supports `UP`, `DOWN`, and `OUT` states. |
| **Pool** | Logical storage group with a configurable replication factor and placement groups (PGs). |
| **Placement Group (PG)** | Subdivides data for balancing. Maps objects to OSDs using a hash of the object ID. |
| **CRUSH Mapping** | Simplified algorithm to select OSDs for each PG and spread replicas across racks. |
| **Monitor** | Tracks OSD states, pool health, and placement mappings. Reports cluster health (`HEALTH_OK`, `HEALTH_WARN`). |
| **RADOS Layer** | Handles object operations: `PUT`, `GET`, `DELETE`, and `LIST`. |
| **Flask Web UI** | Provides a dashboard for file management, OSD control, and real-time cluster status. |

---

## ⚡ How It Works
1. **Cluster Initialization**
   - Starts with **5 OSDs** distributed across 3 racks.
   - Creates 2 default pools:
     - `default` → Data storage.
     - `metadata` → System metadata.

2. **Object Storage**
   - The pool hashes the object ID to pick a **Placement Group (PG)**.
   - CRUSH selects OSDs to store replicas based on the pool’s replication factor (default: 3).
   - Data and metadata (checksum, size, timestamp) are stored on each OSD.

3. **Retrieval**
   - Data is read from the primary OSD (or a replica if the primary is down).
   - Checksum ensures data integrity.

4. **Health Monitoring**
   - If an OSD goes **DOWN**, PGs may enter:
     - `active+degraded` (missing replicas),
     - `inactive` (no active OSDs).
   - Cluster health changes to **HEALTH_WARN**.

---

## 💻 Installation
Clone the repository and install dependencies:
```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
pip install -r requirements.txt
