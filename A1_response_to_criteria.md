# Assignment 1 - REST API Project - Response to Criteria

## Overview
- **Name:** Chelz Chan  
- **Student number:** n10254854  
- **Application name:** Video Splitter  
- **Two line description:** A REST API for uploading, splitting, and retrieving video segments. The system includes JWT-based login, CPU-intensive FFmpeg processing, and a simple web client.

---

## Core criteria

### Containerise the app
- **ECR Repository name:** n10254854-video_splitter  
- **Video timestamp:** 0:28
- **Relevant files:**  
  - Dockerfile  
  - requirements.txt  

### Deploy the container
- **EC2 instance ID:** 901444280953.dkr.ecr.ap-southeast-2.amazonaws.com/n10254854-video-splitter:latest
- **Video timestamp:** 00:40

### User login
- **One line description:** JWT login with hard-coded credentials (admin/admin123, user/user123).  
- **Video timestamp:** 3:51
- **Relevant files:**  
  - app/main.py (auth routes)  
  - static/index.html (login form)  

### REST API
- **One line description:** FastAPI REST API with health, login, video upload, splitting (sync + async), job polling, and listing segments.  
- **Video timestamp:** 2:00
- **Relevant files:**  
  - app/main.py  
  - Auto-generated FastAPI docs (/docs)  

### Data types
- **One line description:** Application stores both unstructured video files and structured metadata/job info.  
- **Video timestamp:** 1:10
- **Relevant files:**  
  - app/main.py  
  - data/  

#### First kind
- **One line description:** Uploaded video files.  
- **Type:** Unstructured data  
- **Rationale:** Input for CPU-intensive FFmpeg splitting.  
- **Video timestamp:** 1:48 
- **Relevant files:**  
  - data/<video_id>/input.mp4  

#### Second kind
- **One line description:** Metadata about jobs, videos, and segments.  
- **Type:** Structured data  
- **Rationale:** Allows tracking jobs and querying segments.  
- **Video timestamp:** 1:20 
- **Relevant files:**  
  - app/main.py (SQLite/job tracking)  

### CPU intensive task
- **One line description:** Video splitting with FFmpeg (heavy filter chain).  
- **Video timestamp:** 4:15
- **Relevant files:**  
  - app/main.py (split_heavy function)  

### CPU load testing
- **One line description:** Load tested with repeated sync split requests; CPU usage >80% confirmed with htop/CloudWatch.  
- **Video timestamp:**   
- **Relevant files:**  
  - load-test script (Hoppscotch/Python)  

---

## Additional criteria

### Extensive REST API features
- **One line description:** Implemented pagination on /videos and ETag support on /segments/{id}.  
- **Video timestamp:** 1:34 
- **Relevant files:**  
  - app/main.py  

### External API(s)
- **One line description:** Not attempted  
- **Video timestamp:** –  
- **Relevant files:** –  

### Additional types of data
- **One line description:** Not attempted  
- **Video timestamp:** –  
- **Relevant files:** –  

### Custom processing
- **One line description:** Not attempted  
- **Video timestamp:** –  
- **Relevant files:** –  

### Infrastructure as code
- **One line description:** Not attempted  
- **Video timestamp:** –  
- **Relevant files:** –  

### Web client
- **One line description:** Browser-based frontend (index.html) supporting login, upload, polling jobs, and segment viewing.  
- **Video timestamp:** 3:16
- **Relevant files:**  
  - static/index.html  

### Upon request
- **One line description:** Not attempted  
- **Video timestamp:** –  
- **Relevant files:** –  
