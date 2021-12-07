import base64
import datetime
import json
import logging
import os
import sys
import time
import traceback

import cattr
import gcsfs
import pystac
from flask import Flask
from flask import request
from loguru import logger
from satextractor.extractor import task_mosaic_patches
from satextractor.models import BAND_INFO
from satextractor.models import ExtractionTask
from satextractor.models import Tile
from satextractor.monitor import GCPMonitor
from satextractor.storer import store_patches

app = Flask(__name__)


if __name__ != "__main__":
    # Redirect Flask logs to Gunicorn logs
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
    app.logger.info("Service started...")
else:
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def format_stacktrace():
    parts = ["Traceback (most recent call last):\n"]
    parts.extend(traceback.format_stack(limit=25)[:-2])
    parts.extend(traceback.format_exception(*sys.exc_info())[1:])
    return "".join(parts)


@app.route("/", methods=["POST"])
def extract_patches():

    try:
        tic = time.time()

        envelope = request.get_json()
        if not envelope:
            msg = "no Pub/Sub message received"
            print(f"error: {msg}")
            return f"Bad Request: {msg}", 400

        if not isinstance(envelope, dict) or "message" not in envelope:
            msg = "invalid Pub/Sub message format"
            print(f"error: {msg}")
            return f"Bad Request: {msg}", 400

        request_json = envelope["message"]["data"]

        if not isinstance(request_json, dict):
            json_data = base64.b64decode(request_json).decode("utf-8")
            request_json = json.loads(json_data)
        # common data
        storage_gs_path = request_json["storage_gs_path"]
        bands = request_json["bands"]
        job_id = request_json["job_id"]

        fs = gcsfs.GCSFileSystem()

        # ExtractionTask data
        extraction_task = request_json["extraction_task"]
        tiles = [cattr.structure(t, Tile) for t in extraction_task["tiles"]]
        item_collection = pystac.ItemCollection.from_dict(
            extraction_task["item_collection"],
        )
        band = extraction_task["band"]
        task_id = extraction_task["task_id"]
        constellation = extraction_task["constellation"]
        sensing_time = datetime.datetime.fromisoformat(extraction_task["sensing_time"])
        task = ExtractionTask(
            task_id,
            tiles,
            item_collection,
            band,
            constellation,
            sensing_time,
        )

        logger.info(f"Ready to extract {len(task.tiles)} tiles.")

        # do monitor if possible
        if "MONITOR_TABLE" in os.environ:
            monitor = GCPMonitor(
                table_name=os.environ["MONITOR_TABLE"],
                storage_path=storage_gs_path,
                job_id=job_id,
                task_id=task_id,
                constellation=constellation,
            )
            monitor.post_status(
                msg_type="STARTED",
                msg_payload=f"Extracting {len(task.tiles)}",
            )
        else:
            logger.warning(
                "Environment variable MONITOR_TABLE not set. Unable to push task status to Monitor",
            )

        archive_resolution = int(
            min([b["gsd"] for _, b in BAND_INFO[constellation].items()]),
        )

        patches = task_mosaic_patches(
            cloud_fs=fs,
            task=task,
            method="max",
            resolution=archive_resolution,
        )

        logger.info(f"Ready to store {len(patches)} patches at {storage_gs_path}.")
        store_patches(
            fs.get_mapper,
            storage_gs_path,
            patches,
            task,
            bands,
            archive_resolution,
        )

        toc = time.time()

        if "MONITOR_TABLE" in os.environ:
            monitor.post_status(
                msg_type="FINISHED",
                msg_payload=f"Elapsed time: {toc-tic}",
            )

        logger.info(
            f"{len(patches)} patches were succesfully stored in {storage_gs_path}.",
        )

        return f"Extracted {len(patches)} patches.", 200

    except Exception as e:

        trace = format_stacktrace()

        if "MONITOR_TABLE" in os.environ:
            monitor.post_status(msg_type="FAILED", msg_payload=trace)

        raise e
