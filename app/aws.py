import io, os, uuid, tempfile, time
import boto3
from botocore.exceptions import ClientError
from .config import AWS_REGION, S3_BUCKET, DDB_VIDEOS_TABLE, DDB_JOBS_TABLE

s3 = boto3.client("s3", region_name=AWS_REGION)
ddb = boto3.client("dynamodb", region_name=AWS_REGION)

# ---------- S3 ----------
def s3_put_bytes(key: str, data: bytes, content_type="application/octet-stream"):
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{S3_BUCKET}/{key}"

def s3_put_file(key: str, local_path: str, content_type=None):
    extra = {"ContentType": content_type} if content_type else {}
    s3.upload_file(local_path, S3_BUCKET, key, ExtraArgs=extra)
    return f"s3://{S3_BUCKET}/{key}"

def s3_get_to_file(key: str, local_path: str):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    s3.download_file(S3_BUCKET, key, local_path)
    return local_path

def s3_presign_get(key: str, expires=3600):
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires
    )

# ---------- DynamoDB ----------
def ddb_put_video(user_email: str, video_id: str, filename: str, duration: float):
    ddb.put_item(
        TableName=DDB_VIDEOS_TABLE,
        Item={
            "qut-username": {"S": user_email},
            "video_id": {"S": video_id},
            "filename": {"S": filename},
            "duration": {"N": str(duration)},
            "created_at": {"N": str(int(time.time()))},
        }
    )

def ddb_list_videos(user_email: str):
    # query by partition key
    resp = ddb.query(
        TableName=DDB_VIDEOS_TABLE,
        KeyConditionExpression="#pk = :u",
        ExpressionAttributeNames={"#pk": "qut-username"},
        ExpressionAttributeValues={":u": {"S": user_email}},
        ScanIndexForward=False
    )
    return resp.get("Items", [])

def ddb_get_video(user_email: str, video_id: str):
    resp = ddb.get_item(
        TableName=DDB_VIDEOS_TABLE,
        Key={"qut-username":{"S":user_email}, "video_id":{"S":video_id}}
    )
    return resp.get("Item")

def ddb_put_job(user_email: str, job_id: str, video_id: str, mode: str, parts: int, status: str):
    ddb.put_item(
        TableName=DDB_JOBS_TABLE,
        Item={
            "qut-username":{"S":user_email},
            "job_id":{"S":job_id},
            "video_id":{"S":video_id},
            "mode":{"S":mode},
            "parts":{"N":str(parts)},
            "status":{"S":status},
            "updated_at":{"N":str(int(time.time()))}
        }
    )

def ddb_update_job_status(user_email: str, job_id: str, status: str, parts_done: int|None=None):
    expr = "SET #s = :s, updated_at = :t"
    names = {"#s":"status"}
    values = {":s":{"S":status}, ":t":{"N":str(int(time.time()))}}
    if parts_done is not None:
        expr += ", parts_done = :pd"
        values[":pd"] = {"N":str(parts_done)}
    ddb.update_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S":user_email}, "job_id":{"S":job_id}},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values
    )

def ddb_get_job(user_email: str, job_id: str):
    resp = ddb.get_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S":user_email}, "job_id":{"S":job_id}}
    )
    return resp.get("Item")
import io, os, uuid, tempfile, time
import boto3
from botocore.exceptions import ClientError
from .config import AWS_REGION, S3_BUCKET, DDB_VIDEOS_TABLE, DDB_JOBS_TABLE

s3 = boto3.client("s3", region_name=AWS_REGION)
ddb = boto3.client("dynamodb", region_name=AWS_REGION)

# ---------- S3 ----------
def s3_put_bytes(key: str, data: bytes, content_type="application/octet-stream"):
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{S3_BUCKET}/{key}"

def s3_put_file(key: str, local_path: str, content_type=None):
    extra = {"ContentType": content_type} if content_type else {}
    s3.upload_file(local_path, S3_BUCKET, key, ExtraArgs=extra)
    return f"s3://{S3_BUCKET}/{key}"

def s3_get_to_file(key: str, local_path: str):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    s3.download_file(S3_BUCKET, key, local_path)
    return local_path

def s3_presign_get(key: str, expires=3600):
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires
    )

# ---------- DynamoDB ----------
def ddb_put_video(user_email: str, video_id: str, filename: str, duration: float):
    ddb.put_item(
        TableName=DDB_VIDEOS_TABLE,
        Item={
            "qut-username": {"S": user_email},
            "video_id": {"S": video_id},
            "filename": {"S": filename},
            "duration": {"N": str(duration)},
            "created_at": {"N": str(int(time.time()))},
        }
    )

def ddb_list_videos(user_email: str):
    # query by partition key
    resp = ddb.query(
        TableName=DDB_VIDEOS_TABLE,
        KeyConditionExpression="#pk = :u",
        ExpressionAttributeNames={"#pk": "qut-username"},
        ExpressionAttributeValues={":u": {"S": user_email}},
        ScanIndexForward=False
    )
    return resp.get("Items", [])

def ddb_get_video(user_email: str, video_id: str):
    resp = ddb.get_item(
        TableName=DDB_VIDEOS_TABLE,
        Key={"qut-username":{"S":user_email}, "video_id":{"S":video_id}}
    )
    return resp.get("Item")

def ddb_put_job(user_email: str, job_id: str, video_id: str, mode: str, parts: int, status: str):
    ddb.put_item(
        TableName=DDB_JOBS_TABLE,
        Item={
            "qut-username":{"S":user_email},
            "job_id":{"S":job_id},
            "video_id":{"S":video_id},
            "mode":{"S":mode},
            "parts":{"N":str(parts)},
            "status":{"S":status},
            "updated_at":{"N":str(int(time.time()))}
        }
    )

def ddb_update_job_status(user_email: str, job_id: str, status: str, parts_done: int|None=None):
    expr = "SET #s = :s, updated_at = :t"
    names = {"#s":"status"}
    values = {":s":{"S":status}, ":t":{"N":str(int(time.time()))}}
    if parts_done is not None:
        expr += ", parts_done = :pd"
        values[":pd"] = {"N":str(parts_done)}
    ddb.update_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S":user_email}, "job_id":{"S":job_id}},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values
    )

def ddb_get_job(user_email: str, job_id: str):
    resp = ddb.get_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S":user_email}, "job_id":{"S":job_id}}
    )
    return resp.get("Item")# app/awx.py
import boto3
from .config import AWS_REGION

_session = boto3.session.Session(region_name=AWS_REGION)
s3 = _session.client("s3")
dynamo = _session.client("dynamodb")
