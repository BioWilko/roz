import boto3
from collections import namedtuple
import configparser
import os
import sys
from io import BytesIO


def get_credentials(args=None):
    __s3_creds = namedtuple(
        "s3_credentials",
        ["access_key", "secret_key", "endpoint", "region", "profile_name"],
    )

    credential_file = configparser.ConfigParser()

    credential_file.read_file(open(os.path.expanduser("~/.aws/credentials"), "rt"))

    if args:
        profile = "default" if not args.profile else args.profile
    else:
        profile = "default"

    endpoint = "https://s3.climb.ac.uk"

    region = "s3"

    if credential_file:
        access_key = credential_file[profile]["aws_access_key_id"]
        secret_key = credential_file[profile]["aws_secret_access_key"]

    if os.getenv("AWS_ACCESS_KEY_ID"):
        access_key = os.getenv("AWS_ACCESS_KEY_ID")

    if os.getenv("AWS_SECRET_ACCESS_KEY"):
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")

    if args:
        if args.access_key:
            access_key = args.access_key

        if args.secret_key:
            secret_key = args.secret_key

    if not access_key or not secret_key:
        error = """CLIMB S3 credentials could not be found, please provide valid credentials in one of the following ways:
            - In a correctly formatted config file (~/.aws/credentials)
            - As environmental variables 'AWS_ACCESS_KEY_ID' and 'AWS_SECRET_ACCESS_KEY'
            - As a command line argument, see --help for more details
        """
        print(error, file=sys.stderr)
        sys.exit(1)

    s3_credentials = __s3_creds(
        access_key=access_key,
        secret_key=secret_key,
        endpoint=endpoint,
        region=region,
        profile_name=profile,
    )

    return s3_credentials


def s3_to_fh(s3_uri):
    s3_credentials = get_credentials()

    bucket = s3_uri.replace("s3://", "").split("/")[0]

    key = s3_uri.replace("s3://", "").split("/", 1)[1]

    s3_client = boto3.s3(
        "s3",
        endpoint_url=s3_credentials.endpoint,
        aws_access_key_id=s3_credentials.access_key,
        region_name=s3_credentials.region,
        aws_secret_access_key=s3_credentials.secret_key,
    )

    file_obj = s3_client.get_object(bucket=bucket, key=key)

    return BytesIO(file_obj["Body"].read())
