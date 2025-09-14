A Python-based distributed file system with web interface that provides file replication, fault tolerance, and automatic load distribution across multiple storage nodes.
🚀 **Features**

Automatic Replication: Files replicated across multiple nodes (configurable)
Fault Tolerance: Handles node failures with automatic failover
Load Distribution: Hash-based file distribution across nodes
Data Integrity: SHA256 checksum verification
Web Interface: Easy-to-use web UI for file operations
REST API: Programmatic access to all operations
Cross-Platform: Works on Windows, macOS, and Linux
No Database Required: Uses file system directly

🏗️ **Architecture**
This is an object-based distributed file system using:

Consistent Hashing for file distribution
Master-less Architecture with no single point of failure
Replication-based Fault Tolerance (default 3 replicas)
Content-addressable Storage with UUID-based file identification

**System Overview**
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Storage       │    │   Storage       │    │   Storage       │
│   Node 1        │    │   Node 2        │    │   Node 3        │
│                 │    │                 │    │                 │
│ ┌─────────────┐ │    │ ┌─────────────┐ │    │ ┌─────────────┐ │
│ │   File A    │ │    │ │   File A    │ │    │ │   File B    │ │
│ │   File C    │ │    │ │   File B    │ │    │ │   File C    │ │
│ └─────────────┘ │    │ └─────────────┘ │    │ └─────────────┘ │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
                    ┌─────────────────────────────┐
                    │     Distributed File        │
                    │     System Coordinator      │
                    │                             │
                    │  • Hash-based routing       │
                    │  • Replication management   │
                    │  • Fault tolerance          │
                    │  • Load balancing          │
                    └─────────────────────────────┘
                                 │
                    ┌─────────────────────────────┐
                    │        Web Interface        │
                    │                             │
                    │  • File upload/download     │
                    │  • System monitoring        │
                    │  • Node management          │
                    │  • REST API                 │
                    └─────────────────────────────┘

# Distributed File System

A simple distributed file system with a web interface.

---

## 🚀 Installation

### 📋 Prerequisites
- Python 3.8 or higher  
- pip (Python package manager)

---

## ⚡ Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/distributed-file-system.git
   cd distributed-file-system
   pip install flask

   python distributer_fs.py

    

