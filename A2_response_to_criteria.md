Assignment 2 - Cloud Services Exercises - Response to Criteria
================================================

Instructions
------------------------------------------------
- Keep this file named A2_response_to_criteria.md, do not change the name
- Upload this file along with your code in the root directory of your project
- Upload this file in the current Markdown format (.md extension)
- Do not delete or rearrange sections.  If you did not attempt a criterion, leave it blank
- Text inside [ ] like [eg. S3 ] are examples and should be removed


Overview
------------------------------------------------

- **Name:** Chelz Chan 
- **Student number:** n10254854 / n9
- **Partner name (if applicable):** Alanna Bui - Nguyen
- **Application name:** Video Splitter
- **Two line description:** We implemented this app that splits videos into x parts
- **EC2 instance name or ID:**

------------------------------------------------

### Core - First data persistence service

- **AWS service name:**  [S3]
- **What data is being stored?:** [Original video files and split video segments]
- **Why is this service suited to this data?:** [S3 is designed for large unstructured binary objects (videos) and scales efficiently for media workloads.]
- **Why is are the other services used not suitable for this data?:** DynamoDB and Parameter Store are not suited for large binary file storage, and RDS would be inefficient and costly for blobs.
- **Bucket/instance/table name:** n10254854-video-splitter
- **Video timestamp:**
- **Relevant files:**  
    - aws.py (s3_upload_stream, s3_get_to_file, s3_put_file, s3_presign_get)  
    - main.py (upload, download endpoints)

### Core - Second data persistence service

- **AWS service name:**  [DynamoDB]
- **What data is being stored?:** Video metadata (filename, duration, created time, S3 key), job status, and output segment keys.
- **Why is this service suited to this data? :** DynamoDB provides fast, serverless, key–value and document storage, ideal for structured metadata and frequent lookups by user/video.
- **Why is are the other services used not suitable for this data?:** S3 doesn’t provide efficient querying, RDS would add unnecessary management overhead for simple key-value lookups.
- **Bucket/instance/table name:** n10254854-videos (videos), n10254854-jobs (jobs)
- **Video timestamp:**
- **Relevant files:** 
    - aws.py (ddb_put_video, ddb_get_video, ddb_put_job, ddb_update_job_status, etc.) 
    - main.py (list_videos, job_status, etc.)

### Third data service

- **AWS service name:**  [AWS Systems Manager Parameter Store]
- **What data is being stored?:** [Configuration values such as bucket names, DynamoDB table names, JWT secrets, and API base URLs.]
- **Why is this service suited to this data?:** [Secure and centralised configuration management with caching and version control.]
- **Why is are the other services used not suitable for this data?:** [S3 is not designed for secrets/config; DynamoDB is overkill for key-value configuration; RDS would be excessive.]
- **Bucket/instance/table name:** /n10254854/video-splitter/prod/*
- **Video timestamp:**
- **Relevant files:** 
    - config.py (uses get_param from SSM) ssm_params.py

### S3 Pre-signed URLs

- **S3 Bucket names:** n10254854-video-splitter
- **Video timestamp:**
- **Relevant files:**
    - aws.py (s3_presign_get, make_presigned_url)
    - main.py (download_s3 endpoint, get_video)

### In-memory cache

- **ElastiCache instance name:**
- **What data is being cached?:** [eg. Thumbnails from YouTube videos obatined from external API]
- **Why is this data likely to be accessed frequently?:** [ eg. Thumbnails from popular YouTube videos are likely to be shown to multiple users ]
- **Video timestamp:**
- **Relevant files:**
    -

### Core - Statelessness

- **What data is stored within your application that is not stored in cloud data services?:** [Temporary local files created during video processing (ffmpeg split jobs, probes).]
- **Why is this data not considered persistent state?:** [They are intermediate artefacts that can always be re-generated from the original video in S3.]
- **How does your application ensure data consistency if the app suddenly stops?:** [Jobs and outputs are tracked in DynamoDB; if the app crashes, uploaded files remain in S3 and job metadata persists in DynamoDB for recovery.]
- **Relevant files:**
    -main.py (split_video_locally_and_upload, run_split_job with tempfile usage)
    routes_videos.py (tempfile in upload & split)

### Graceful handling of persistent connections

- **Type of persistent connection and use:** [Streaming connections for file downloads (StreamingResponse from S3).]
- **Method for handling lost connections:** [Client reconnects with Range headers to resume from S3; presigned URL remains valid for 1 hour.]
- **Relevant files:**
    -main.py (download_s3 endpoint)


### Core - Authentication with Cognito

- **User pool name:**
- **How are authentication tokens handled by the client?:** [eg. Response to login request sets a cookie containing the token.]
- **Video timestamp:**
- **Relevant files:**
    -

### Cognito multi-factor authentication

- **What factors are used for authentication:** [eg. password, SMS code]
- **Video timestamp:**
- **Relevant files:**
    -

### Cognito federated identities

- **Identity providers used:**
- **Video timestamp:**
- **Relevant files:**
    -

### Cognito groups

- **How are groups used to set permissions?:** [eg. 'admin' users can delete and ban other users]
- **Video timestamp:**
- **Relevant files:**
    -

### Core - DNS with Route53

- **Subdomain**:  [n10254854.cab432.com]
- **Video timestamp:**

### Parameter store

- **Parameter names:** [/n10254854/video-splitter/prod/s3/bucket

/n10254854/video-splitter/prod/ddb/videos_table

/n10254854/video-splitter/prod/ddb/jobs_table

/n10254854/video-splitter/prod/auth/jwt_secret

/n10254854/video-splitter/prod/api/base_url]
- **Video timestamp:**
- **Relevant files:**
    -config.py
    - ssm_params.py

### Secrets manager

- **Secrets names:** [eg. n1234567-youtube-api-key]
- **Video timestamp:**
- **Relevant files:**
    -

### Infrastructure as code

- **Technology used:**
- **Services deployed:**
- **Video timestamp:**
- **Relevant files:**
    -

### Other (with prior approval only)

- **Description:**
- **Video timestamp:**
- **Relevant files:**
    -

### Other (with prior permission only)

- **Description:**
- **Video timestamp:**
- **Relevant files:**
    -