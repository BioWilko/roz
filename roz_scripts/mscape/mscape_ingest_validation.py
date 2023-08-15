import csv
import os
import subprocess
from pathlib import Path
import json
import copy
import boto3
from botocore.exceptions import ClientError
import time
import logging

import utils.utils as utils
from varys import varys, init_logger

from onyx import OnyxClient
import requests


class pipeline:
    def __init__(
        self,
        pipe: str,
        config: Path,
        nxf_executable: Path,
        profile=None,
        timeout_seconds=3600,
    ):
        """
        Run a nxf pipeline as a subprocess, this is only advisable for use with cloud executors, specifically k8s.
        If local execution is needed then you should use something else.

        Args:
            pipe (str): The pipeline to run as a github repo in the format 'user/repo'
            config (str): Path to a nextflow config file
            nxf_executable (str): Path to the nextflow executable
            profile (str): The nextflow profile to use
            timeout_seconds (int): The number of seconds to wait before timing out the pipeline

        """

        self.pipe = pipe
        self.config = Path(config) if config else None
        self.nxf_executable = (
            Path(nxf_executable).resolve()
            if nxf_executable != "nextflow"
            else "nextflow"
        )
        self.profile = profile
        self.timeout_seconds = timeout_seconds

    def execute(self, params: dict) -> tuple[int, bool, str, str]:
        """Execute the pipeline with the given parameters

        Args:
            params (dict): A dictionary of parameters to pass to the pipeline in the format {'param_name': 'param_value'} (no --)

        Returns:
            tuple[int, bool, str, str]: A tuple containing the return code, a bool indicating whether the pipeline timed out, stdout and stderr
        """
        timeout = False

        cmd = [self.nxf_executable, "run", "-r", "main", "-latest", self.pipe]

        if self.config:
            cmd.extend(["-c", self.config.resolve()])

        if self.profile:
            cmd.extend(["-profile", self.profile])

        if params:
            for k, v in params.items():
                cmd.extend([f"--{k}", v])

        try:
            proc = subprocess.run(
                args=cmd,
                capture_output=True,
                universal_newlines=True,
                text=True,
                timeout=self.timeout_seconds,
            )

        except subprocess.TimeoutExpired:
            timeout = True

        return (proc.returncode, timeout, proc.stdout, proc.stderr)


def execute_validation_pipeline(
    payload: dict, args: argparse.Namespace, log: logging.Logger, ingest_pipe: pipeline
) -> tuple[int, bool, str, str]:
    """Execute the validation pipeline for a given artifact

    Args:
        payload (dict): The payload dict for the current artifact
        args (argparse.Namespace): The command line arguments object
        log (logging.Logger): The logger object
        ingest_pipe (pipeline): The instance of the ingest pipeline (see pipeline class)

    Returns:
        tuple[int, bool, str, str]: A tuple containing the return code, a bool indicating whether the pipeline timed out, stdout and stderr
    """

    parameters = {
        "out_dir": args.result_dir,
        "unique_id": payload["uuid"],
        "climb": "",
        "max_human_reads_before_rejection": "10000",
        "k2_host": "10.1.185.58",  # Parameterise this and deal with DNS stuff
        "k2_port": "8080",
        "db": "/shared/public/db/kraken2/k2_pluspf/",
    }

    if payload["platform"] == "ont":
        parameters["fastq"] = payload["files"][".fastq.gz"]["uri"]

    elif payload["platform"] == "illumina":
        parameters["fastq1"] = payload["files"][".1.fastq.gz"]["uri"]
        parameters["fastq2"] = payload["files"][".2.fastq.gz"]["uri"]
        parameters["paired"] = ""
    else:
        log.error(
            f"Unrecognised platform: {payload['platform']} for UUID: {payload['uuid']}"
        )

    log.info(f"Submitted ingest pipeline for UUID: {payload['uuid']}")

    return ingest_pipe.execute(params=parameters)


def onyx_submission(
    log: logging.Logger,
    payload: dict,
    varys_client: varys,
) -> tuple[bool, dict]:
    """_Description:_
    This function is responsible for submitting a record to Onyx, it is called from the main ingest function
    when a match is found for a given artifact.

    Args:
        log (logging.Logger): The logger object
        payload (dict): The payload dict of the currently ingesting artifact
        varys_client (varys.varys): A varys client object

    Returns:
        tuple[bool, dict]: A tuple containing a bool indicating the status of the Onyx create request and the payload dict modified to include information about the Onyx create request
    """

    with OnyxClient(env_password=True) as client:
        log.info(
            f"Received match for artifact: {payload['artifact']}, now attempting to create record in Onyx"
        )

        try:
            response_generator = client.csv_create(
                payload["project"],
                csv_file=utils.s3_to_fh(
                    payload["files"][".csv"]["uri"],
                    payload["files"][".csv"]["etag"],
                ),
            )

            response = next(response_generator)

            payload["onyx_status_code"] = response.status_code

            if response.status_code == 500:
                log.error(
                    f"Onyx create for UUID: {payload['uuid']} lead to onyx internal server error"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    "Onyx internal server error"
                ]

            elif response.status_code == 422:
                log.error(
                    f"Onyx create for UUID: {payload['uuid']} failed, details in validated messages"
                )
                ingest_fail = True
                if response.json().get("messages"):
                    for field, messages in response.json()["messages"].items():
                        if payload["onyx_errors"].get(field):
                            payload["onyx_errors"][field].extend(messages)
                        else:
                            payload["onyx_errors"][field] = messages

            elif response.status_code == 404:
                log.error(
                    f"Onyx create for UUID: {payload['uuid']} failed because project: {payload['project']} does not exist"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    f"Project {payload['project']} does not exist"
                ]

            elif response.status_code == 403:
                log.error(
                    f"Onyx create for UUID: {payload['uuid']} failed due to a permission error"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    "Bad fields in onyx create request"
                ]

            elif response.status_code == 401:
                log.error(
                    f"Onyx create for UUID: {payload['uuid']} failed due to incorrect credentials"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    "Incorrect Onyx credentials"
                ]

            elif response.status_code == 400:
                log.error(
                    f"Onyx create for UUID: {payload['uuid']} failed due to a malformed request (should not happen ever)"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    "Malformed onyx create request"
                ]

            elif response.status_code == 200:
                log.error(
                    f"Onyx responded with 200 on a create request for UUID: {payload['uuid']} (this should be 201)"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    "200 response status on onyx create (should be 201)"
                ]

            elif response.status_code == 201:
                log.info(
                    f"Successful create for UUID: {payload['uuid']} which has been assigned CID: {response.json()['data']['cid']}"
                )
                payload["onyx_create_status"] = True
                payload["created"] = True
                payload["cid"] = response.json()["data"]["cid"]

            else:
                log.error(
                    f"Unhandled Onyx response status code {response.status_code} from Onyx create for UUID: {payload['uuid']}"
                )
                ingest_fail = True
                payload["onyx_errors"]["onyx_client_errors"] = [
                    f"Unhandled response status code {response.status_code} from Onyx create"
                ]

            if ingest_fail:
                varys_client.send(
                    message=payload,
                    exchange=f"inbound.results.mscape.{payload['site_code']}",
                    queue_suffix="validator",
                )

        except Exception as e:
            log.error(
                f"Onyx CSV create failed for UUID: {payload['uuid']} due to client error: {e}"
            )
            payload["onyx_errors"]["onyx_client_errors"] = [
                f"Unhandled client error {e}"
            ]

    return (ingest_fail, payload)


def add_taxon_records(
    payload: dict, result_path: str, log: logging.Logger, s3_client: boto3.client
) -> tuple[bool, dict]:
    """Function to add nested taxon records to an existing Onyx record from a Scylla reads_summary.json file

    Args:
        payload (dict): Dict containing the payload for the current artifact
        result_path (str): Result path for the current artifact
        log (logging.Logger): Logger object
        s3_client (boto3.client): Boto3 client object for S3

    Returns:
        tuple[bool, dict]: Tuple containing a bool indicating whether the upload failed and the updated payload dict
    """
    nested_records = []
    binned_read_fail = False

    with open(
        os.path.join(result_path, "reads_by_taxa/reads_summary.json"), "rt"
    ) as read_summary_fh:
        summary = json.load(read_summary_fh)

        for taxa in summary:
            taxon_dict = {
                "taxon_id": taxa["taxon_id"],
                "human_readable": taxa["human_readable"],
                "n_reads": taxa["qc_metrics"]["num_reads"],
                "avg_quality": taxa["qc_metrics"]["avg_qual"],
                "mean_len": taxa["qc_metrics"]["mean_len"],
                "tax_level": taxa["tax_level"],
            }

            if payload["platform"] == "illumina":
                for i in (1, 2):
                    fastq_path = os.path.join(
                        result_path,
                        f"reads_by_taxa/{taxa['filenames'][i - 1]}.gz",
                    )

                    try:
                        s3_client.upload_file(
                            fastq_path,
                            "mscapetest-published-binned-reads",
                            f"{payload['cid']}/{taxa['taxon']}_{i}.fastq.gz",
                        )

                        taxon_dict[
                            f"reads_{i}"
                        ] = f"s3://mscapetest-published-binned-reads/{payload['cid']}/{taxa['taxon']}_{i}.fastq.gz"

                    except ClientError as e:
                        log.error(
                            f"Failed to binned reads for taxon {taxa['taxon']} to long-term storage bucket for UUID: {payload['uuid']} with CID: {payload['cid']} due to client error: {e}"
                        )
                        payload["ingest_errors"].append(
                            f"Failed to upload binned reads for taxon: {taxa['taxon']} to storage bucket"
                        )
                        binned_read_fail = True
                        continue

            elif payload["platform"] == "ont":
                fastq_path = os.path.join(
                    result_path, f"reads_by_taxa/{taxa['filenames'][0]}.gz"
                )

                try:
                    s3_client.upload_file(
                        fastq_path,
                        "mscapetest-published-binned-reads",
                        f"{payload['cid']}/{taxa['taxon']}.fastq.gz",
                    )

                    taxon_dict[
                        f"reads_1"
                    ] = f"s3://mscapetest-published-binned-reads/{payload['cid']}/{taxa['taxon']}.fastq.gz"

                except ClientError as e:
                    log.error(
                        f"Failed to binned reads for taxon {taxa['taxon']} to long-term storage bucket for UUID: {payload['uuid']} with CID: {payload['cid']} due to client error: {e}"
                    )
                    payload["ingest_errors"].append(
                        f"Failed to upload binned reads for taxon: {taxa['taxon']} to storage bucket"
                    )
                    binned_read_fail = True
                    continue

            else:
                binned_read_fail = True

            nested_records.append(taxon_dict)

    with OnyxClient(env_password=True) as client:
        try:
            response = client.update(
                project="mscapetest",
                cid=payload["cid"],
                fields={"taxa": nested_records},
            )

            if response.status_code == 200:
                log.info(
                    f"Successfully updated Onyx record for CID: {payload['cid']} with nested taxon records"
                )
            else:
                log.error(
                    f"Failed to update Onyx record for CID: {payload['cid']} with status code: {response.status_code}"
                )
                if response.json().get("messages"):
                    for field, messages in response.json()["messages"].items():
                        if payload["onyx_errors"].get(field):
                            payload["onyx_errors"][field].extend(messages)
                        else:
                            payload["onyx_errors"][field] = messages
                binned_read_fail = True

        except Exception as e:
            log.error(
                f"Onyx CSV create failed for UUID: {payload['uuid']} due to client error: {e}"
            )
            payload["onyx_errors"]["onyx_client_errors"] = [
                f"Unhandled client error {e}"
            ]
            binned_read_fail = True

    return (binned_read_fail, payload)


def push_report_file(
    payload: dict, result_path: str, log: logging.Logger, s3_client: boto3.client
) -> tuple[bool, dict]:
    """Push report file to long-term storage bucket and update the Onyx record with the report URI

    Args:
        payload (dict): Payload dict for the current artifact
        result_path (str): Path to the results directory
        log (logging.Logger): Logger object
        s3_client (boto3.client): Boto3 client object for S3

    Returns:
        tuple[bool, dict]: Tuple containing a bool indicating whether the upload failed and the updated payload dict
    """

    report_fail = False

    report_path = os.path.join(result_path, f"{payload['uuid']}_report.html")
    try:
        # Add handling for Db in name etc
        s3_client.upload_file(
            report_path,
            "mscapetest-published-reports",
            f"{payload['cid']}_scylla_report.html",
        )
    except ClientError as e:
        log.error(
            f"Failed to upload scylla report to long-term storage bucket for UUID: {payload['uuid']} with CID: {payload['cid']} due to client error: {e}"
        )
        payload["ingest_errors"].append(
            f"Failed to upload scylla report to storage bucket"
        )
        report_fail = True

    with OnyxClient(env_password=True) as client:
        try:
            response = client.update(
                project="mscapetest",
                cid=payload["cid"],
                fields={
                    "validation_report": f"s3://mscapetest-published-reports/{payload['cid']}_validation_report.html"
                },
            )

            if response.status_code == 200:
                log.info(
                    f"Successfully updated Onyx record for CID: {payload['cid']} with report"
                )
            else:
                log.error(
                    f"Failed to update Onyx record for CID: {payload['cid']} with status code: {response.status_code}"
                )
                if response.json().get("messages"):
                    for field, messages in response.json()["messages"].items():
                        if payload["onyx_errors"].get(field):
                            payload["onyx_errors"][field].extend(messages)
                        else:
                            payload["onyx_errors"][field] = messages
                report_fail = True

        except Exception as e:
            log.error(
                f"Onyx CSV create failed for UUID: {payload['uuid']} due to client error: {e}"
            )
            payload["onyx_errors"]["onyx_client_errors"] = [
                f"Unhandled client error {e}"
            ]
            report_fail = True

    return (report_fail, payload)


def add_reads_record(
    cid: str,
    payload: dict,
    s3_client: boto3.client,
    result_path: str,
    log: logging.Logger,
) -> tuple[bool, dict]:
    """Function to upload raw reads to long-term storage bucket and add the reads_1 and reads_2 fields to the Onyx record

    Args:
        cid (str): CID for the record to update
        payload (dict): Payload dict for the record to update
        s3_client (boto3.client): Boto3 client object for S3
        result_path (str): Path to the results directory
        log (logging.Logger): Logger object

    Returns:
        tuple[bool, dict]: Tuple containing a bool indicating whether the upload failed and the updated payload dict
    """

    raw_read_fail = False

    if payload["platform"] == "illumina":
        for i in (1, 2):
            fastq_path = os.path.join(
                result_path, f"preprocess/{payload['uuid']}_{i}.fastp.fastq.gz"
            )

            try:
                s3_client.upload_file(
                    fastq_path,
                    "mscapetest-published-reads",
                    f"{payload['cid']}_{i}.fastq.gz",
                )

            except ClientError as e:
                log.error(
                    f"Failed to upload reads to long-term storage bucket for UUID: {payload['uuid']} with CID: {payload['cid']} due to client error: {e}"
                )
                payload["ingest_errors"].append(
                    f"Failed to upload reads to storage bucket"
                )
                raw_read_fail = True
                continue

        if not raw_read_fail:
            with OnyxClient(env_password=True) as client:
                try:
                    response = client.update(
                        project="mscapetest",
                        cid=cid,
                        fields={
                            "reads_1": f"s3://mscapetest-published-reads/{payload['cid']}_1.fastq.gz",
                            "reads_2": f"s3://mscapetest-published-reads/{payload['cid']}_2.fastq.gz",
                        },
                    )
                    return response
                except requests.HTTPError as e:
                    raw_read_fail = True

    else:
        fastq_path = os.path.join(
            result_path, f"preprocess/{payload['uuid']}.fastp.fastq.gz"
        )

        try:
            s3_client.upload_file(
                fastq_path,
                "mscapetest-published-reads",
                f"{payload['cid']}.fastq.gz",
            )

        except ClientError as e:
            log.error(
                f"Failed to upload reads to long-term storage bucket for UUID: {payload['uuid']} with CID: {payload['cid']} due to client error: {e}"
            )
            payload["ingest_errors"].append(f"Failed to upload reads to storage bucket")

            raw_read_fail = True

        if not raw_read_fail:
            with OnyxClient(env_password=True) as client:
                try:
                    response = client.update(
                        project="mscapetest",
                        cid=cid,
                        fields={
                            "reads_1": f"s3://mscapetest-published-reads/{payload['cid']}_1.fastq.gz"
                        },
                    )
                    return response
                except requests.HTTPError as e:
                    raw_read_fail = True

    return (raw_read_fail, payload)


def ret_0_parser(
    log: logging.logger,
    payload: dict,
    message: dict,
    result_path: str,
    varys_client: varys,
    ingest_fail: bool = False,
) -> tuple[bool, dict]:
    """Function to parse the execution trace of a Nextflow pipeline run to determine whether any of the processes failed.

    Args:
        log (logging.logger): Logger object
        payload (dict): Payload dictionary
        message (dict): Message dictionary
        result_path (str): Path to the results directory
        varys_client (varys): Varys client object
        ingest_fail (bool): Boolean to indicate whether the ingest has failed up to this point (default: False)

    Returns:
        tuple[bool, dict]: Tuple containing the ingest fail boolean and the payload dictionary
    """

    try:
        with open(
            os.path.join(
                result_path,
                "pipeline_info",
                f"execution_trace_{payload['uuid']}.txt",
            )
        ) as trace_fh:
            reader = csv.DictReader(trace_fh, delimiter="\t")

            trace_dict = {}
            for process in reader:
                trace_dict[process["name"].split(":")[-1]] = process

    except Exception as e:
        log.error(
            f"Could not open pipeline trace for UUID: {payload['uuid']} despite NXF exit code 0 due to error: {e}"
        )
        payload["ingest_errors"].append("couldn't open nxf ingest pipeline trace")
        varys_client.send(
            message=payload,
            exchange=f"inbound.results.mscape.{message['site']}",
            queue_suffix="validator",
        )
        ingest_fail = True

    for process, trace in trace_dict.items():
        if trace["exit"] != "0":
            if (
                process == "extract_paired_reads"
                or process == "extract_reads"
                and trace["exit"] == "2"
            ):
                payload["ingest_errors"].append(
                    "Human reads detected above rejection threshold, please ensure pre-upload dehumanisation has been performed properly"
                )
                ingest_fail = True
            else:
                payload["ingest_errors"].append(
                    f"MScape validation pipeline (Scylla) failed in process {process} with exit code {trace['exit']} and status {trace['status']}"
                )
                ingest_fail = True

    return (ingest_fail, payload)


def onyx_unsuppress():
    unsuppress_fail = False
    with OnyxClient(env_password=True) as client:
        # Unsuppress the record
        pass


def run(args):
    # Setup producer / consumer
    log = varys.init_logger("mscape.ingest", args.logfile, args.log_level)

    varys_client = varys(
        profile="roz",
        in_exchange="inbound.to_validate",
        out_exchange="inbound.validated.mscape",
        logfile=args.logfile,
        log_level=args.log_level,
        queue_suffix="validator",
        config_path="/home/jovyan/roz_profiles.json",
    )

    validation_payload_template = {
        "uuid": "",
        "artifact": "",
        "project": "",
        "ingest_timestamp": "",
        "cid": False,
        "site": "",
        "created": False,
        "ingested": False,
        "onyx_test_status_code": False,
        "onyx_test_create_errors": {},  # Dict
        "onyx_test_create_status": False,
        "onyx_status_code": False,
        "onyx_errors": {},  # Dict
        "onyx_create_status": False,
        "ingest_errors": [],  # List,
        "test_flag": True,  # Add this throughout
        "test_ingest_result": False,
    }

    ingest_pipe = pipeline(
        pipe="snowy-leopard/scylla",
        profile="docker",
        config=args.nxf_config,
        nxf_executable=args.nxf_executable,
    )

    s3_client = boto3.client(
        "s3",
        endpoint_url="https://s3.climb.ac.uk",
        aws_access_key_id=os.getenv("ROZ_AWS_ACCESS"),
        aws_secret_access_key=os.getenv("ROZ_AWS_SECRET"),
    )

    while True:
        message = varys_client.receive(
            exchange="inbound.to_validate", queue_suffix="validator"
        )

        to_validate = json.loads(message.body)

        payload = copy.deepcopy(to_validate)

        # This client is purely for Mscape, ignore all other messages
        if to_validate["project"] != "mscapetest":
            log.info(
                f"Ignoring file set with UUID: {message['uuid']} due non-mscape project ID"
            )
            continue

        if not to_validate["onyx_test_create_status"]:
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        rc, timeout, stdout, stderr = execute_validation_pipeline(
            payload=payload, args=args, log=log, ingest_pipe=ingest_pipe
        )

        if not timeout:
            log.info(f"Pipeline execution for message id: {payload['uuid']}, complete.")
        else:
            log.error(f"Pipeline execution timed out for message id: {payload['uuid']}")
            payload["ingest_errors"].append("Validation pipeline timeout")
            log.info(f"Sending validation result for UUID: {payload['uuid']}")
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        result_path = os.path.join(args.result_dir.resolve(), payload["uuid"])

        if not os.path.exists(result_path):
            os.makedirs(result_path)

        with open(os.path.join(result_path, "nextflow.stdout"), "wt") as out_fh, open(
            os.path.join(result_path, "nextflow.stderr"), "wt"
        ) as err_fh:
            out_fh.write(stdout)
            err_fh.write(stderr)

        if rc != 0:
            log.error(
                f"Scylla exited with non-0 exit code: {rc} for UUID: {payload['uuid']}"
            )
            payload["ingest_errors"].append(f"Scylla exited with non-0 exit code: {rc}")
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        ingest_fail, payload = ret_0_parser(
            log=log,
            payload=payload,
            message=message,
            result_path=result_path,
            varys_client=varys_client,
        )

        if payload["test_flag"]:
            log.info(
                f"Test ingest for artifact: {payload['artifact']} with UUID: {payload['uuid']} completed successfully"
            )
            payload["test_ingest_result"] = True
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        ingest_fail, payload = onyx_submission(
            log=log,
            payload=payload,
            varys_client=varys_client,
            ingest_fail=ingest_fail,
        )

        if ingest_fail:
            log.info(f"Failed to submit to Onyx for UUID: {payload['uuid']}")
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        log.info(
            f"Uploading files to long-term storage buckets for CID: {payload['cid']} after sucessful Onyx submission"
        )

        raw_read_fail, payload = add_reads_record(
            cid=payload["cid"],
            payload=payload,
            s3_client=s3_client,
            result_path=result_path,
            log=log,
        )

        if raw_read_fail:
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )

        binned_read_fail, payload = add_taxon_records(
            payload=payload, result_path=result_path, log=log, s3_client=s3_client
        )

        if binned_read_fail:
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )

        report_fail, payload = push_report_file(
            payload=payload, result_path=result_path, log=log, s3_client=s3_client
        )

        if report_fail:
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )

        if raw_read_fail or binned_read_fail or report_fail:
            log.error(
                f"Failed to upload at least one file to long-term storage for CID: {payload['cid']}"
            )
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        unsuppress_fail, payload = onyx_unsuppress()

        if unsuppress_fail:
            log.error(f"Failed to unsuppress Onyx record for CID: {payload['cid']}")
            varys_client.send(
                message=payload,
                exchange=f"inbound.results.mscape.{message['site']}",
                queue_suffix="validator",
            )
            continue

        payload["ingested"] = True
        log.info(
            f"Sending successful ingest result for UUID: {payload['uuid']}, with CID: {payload['cid']}"
        )

        new_artifact_payload = {
            "ingest_timestamp": time.time_ns(),
            "cid": payload["cid"],
            "site": payload["site"],
            "match_uuid": payload["uuid"],
        }

        varys_client.send(
            message=new_artifact_payload,
            exchange="inbound.new_artifact.mscape",
            queue_suffix="validator",
        )

        varys_client.send(
            message=payload,
            exchange=f"inbound.results.mscape.{message['site']}",
            queue_suffix="validator",
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--logfile", type=Path)
    parser.add_argument("--log_level", type=str, default="DEBUG")
    parser.add_argument("--nxf_config")
    parser.add_argument("--work_bucket")
    parser.add_argument("--nxf_executable", default="nextflow")
    parser.add_argument("--result_dir", type=Path)
    parser.add_argument("--temp_dir", type=Path)
    args = parser.parse_args()

    run(args)