# app/aws.py
import os
import time
import boto3
from botocore.exceptions import ClientError
from .config import AWS_REGION, S3_BUCKET, DDB_VIDEOS_TABLE, DDB_JOBS_TABLE

# One session/clients
_session = boto3.session.Session(region_name=AWS_REGION)
s3 = _session.client("s3")
ddb = _session.client("dynamodb")

# ---------- S3 ----------
def s3_upload_stream(fileobj, key: str, content_type="application/octet-stream"):
    # Stream straight to S3
    s3.upload_fileobj(fileobj, S3_BUCKET, key, ExtraArgs={"ContentType": content_type})
    return f"s3://{S3_BUCKET}/{key}"

def s3_put_file(key: str, local_path: str, content_type=None):
    extra = {"ContentType": content_type} if content_type else {}
    s3.upload_file(local_path, S3_BUCKET, key, ExtraArgs=extra)
    return f"s3://{S3_BUCKET}/{key}"

def s3_get_to_file(key: str, local_path: str):
    import os
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    s3.download_file(S3_BUCKET, key, local_path)
    return local_path

def s3_presign_get(key: str, expires=3600):
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires
    )

def s3_list_first_key(prefix: str) -> str | None:
    """
    Return the first object key under the given prefix, or None if nothing is there.
    """
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=1)
    contents = resp.get("Contents")
    if not contents:
        return None
    return contents[0]["Key"]

# ---------- DynamoDB: Videos ----------
def ddb_put_video(user_email: str, video_id: str, filename: str, duration: float, s3_key: str | None = None):
    item = {
        "qut-username": {"S": user_email},
        "video_id": {"S": video_id},
        "filename": {"S": filename},
        "duration": {"N": str(duration)},
        "created_at": {"N": str(int(time.time()))},
    }
    if s3_key:
        item["s3_key"] = {"S": s3_key}
    ddb.put_item(TableName=DDB_VIDEOS_TABLE, Item=item)

def ddb_set_video_s3_key(user_email: str, video_id: str, s3_key: str):
    ddb.update_item(
        TableName=DDB_VIDEOS_TABLE,
        Key={"qut-username":{"S":user_email}, "video_id":{"S":video_id}},
        UpdateExpression="SET s3_key = :k, updated_at = :t",
        ExpressionAttributeValues={
            ":k": {"S": s3_key},
            ":t": {"N": str(int(time.time()))},
        },
    )

def ddb_list_videos(user_email: str, limit: int = 25, last_key=None):
    kwargs = {
        "TableName": DDB_VIDEOS_TABLE,
        "KeyConditionExpression": "#pk = :u",
        "ExpressionAttributeNames": {"#pk": "qut-username"},
        "ExpressionAttributeValues": {":u": {"S": user_email}},
        "ScanIndexForward": False,  # newest first (by sort key = video_id or created_at GSI if you have one)
        "Limit": limit,
    }
    if last_key:
        kwargs["ExclusiveStartKey"] = last_key
    return ddb.query(**kwargs)

def s3_key_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False
    
def ddb_get_video(user_email: str, video_id: str):
    vid = (video_id or "").strip()

    # 1) Ideal path: PK+SK with strong consistency
    try:
        resp = ddb.get_item(
            TableName=DDB_VIDEOS_TABLE,
            Key={"qut-username": {"S": user_email}, "video_id": {"S": vid}},
            ConsistentRead=True,
        )
        if "Item" in resp and resp["Item"]:
            return resp["Item"]
    except Exception:
        pass  # fall through

    # 2) Safety fallback: query the partition and find the matching video_id
    try:
        resp = ddb.query(
            TableName=DDB_VIDEOS_TABLE,
            KeyConditionExpression="#pk = :u",
            ExpressionAttributeNames={"#pk": "qut-username"},
            ExpressionAttributeValues={":u": {"S": user_email}},
            ScanIndexForward=False,
            Limit=200,
            ConsistentRead=True,
        )
        for it in resp.get("Items", []):
            if it.get("video_id", {}).get("S") == vid:
                return it
    except Exception:
        pass

    return None


def ddb_append_outputs(user_email: str, video_id: str, mode: str, parts: int, segment_keys: list[str]):
    """
    Appends one outputs-group to the video item:
      outputs = list of maps { mode: S, parts: N, segments: list<S> }
    """
    ddb.update_item(
        TableName=DDB_VIDEOS_TABLE,
        Key={"qut-username":{"S":user_email}, "video_id":{"S":video_id}},
        UpdateExpression="SET #o = list_append(if_not_exists(#o, :empty), :val), updated_at = :t",
        ExpressionAttributeNames={"#o": "outputs"},
        ExpressionAttributeValues={
            ":empty": {"L": []},
            ":val": {"L": [
                {"M": {
                    "mode": {"S": mode},
                    "parts": {"N": str(parts)},
                    "segments": {"L": [{"S": k} for k in segment_keys]}
                }}
            ]},
            ":t": {"N": str(int(time.time()))},
        },
        ReturnValues="NONE"
    )

# ---------- DynamoDB: Jobs ----------
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

def ddb_update_job_status(user_email: str, job_id: str, status: str,
                          parts_done: int | None = None, error: str | None = None):
    expr = "SET #s = :s, updated_at = :t"
    names = {"#s": "status"}
    values = {":s": {"S": status}, ":t": {"N": str(int(time.time()))}}
    if parts_done is not None:
        expr += ", parts_done = :pd"
        values[":pd"] = {"N": str(parts_done)}
    if error is not None:
        # 'error' is reserved → use a name placeholder
        expr += ", #err = :e"
        names["#err"] = "error"
        values[":e"] = {"S": error[:500]}
    ddb.update_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S":user_email}, "job_id":{"S":job_id}},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )

def ddb_get_job(user_email: str, job_id: str):
    resp = ddb.get_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S":user_email}, "job_id":{"S":job_id}}
    )
    return resp.get("Item")
