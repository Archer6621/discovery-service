#!/usr/bin/env python

import json

from celery import chord, group
from celery.result import result_from_tuple
from flask import Response
from flask import request
from flask_restx import Resource, fields

from backend import api
# Import own modules
from backend import app
from backend.discovery.queries import delete_spurious_connections, get_related_between_two_tables
from backend.profiling.metanome import profile_metanome
from backend.utility.celery_tasks import *
from backend.utility.celery_utils import generate_status_tree
from backend.utility.display import log_format
from backend.search import redis_tools as db

# Display/logging settings
logging.basicConfig(format=log_format, level=logging.INFO)

TaskIdModel = api.model('TaskId', {'task_id': fields.String, 'type': fields.String})


@api.route('/ingest-data')
@api.doc(description="Ingest all the data located at the given bucket.")
@api.doc(params={
    'bucket': {'description': 'Path to the S3 bucket with data', 'in': 'query', 'type': 'string', 'required': 'true',
               'default': 'data'},
})
class IngestData(Resource):
    @api.response(202, 'Success, processing in backend', TaskIdModel)
    @api.response(204, 'Bucket is empty')
    @api.response(400, 'Missing bucket query parameter')
    @api.response(404, 'Bucket does not exist')
    def get(self):
        bucket = request.args.get("bucket")
        if not bucket:
            return Response('Missing bucket query parameter', status=400)

        if not search.io_tools.bucket_exists(bucket):
            return Response("Bucket does not exist", status=404)

        paths = search.io_tools.get_tables(bucket)
        if len(paths) == 0:
            return Response("Bucket is empty", status=204)

        header = []
        for table_path in paths:
            if not db.table_exists(table_path):
                header.append(add_table.s(bucket, table_path))
            else:
                logging.info(f"Table {table_path} was already processed!")

        task_group = group(*header)
        profiling_chord = chord(task_group)(profile_valentine_all.si(bucket))
        profiling_chord.parent.save()
        db.save_celery_task(profiling_chord.id, profiling_chord.as_tuple())

        return Response(json.dumps({"task_id": profiling_chord.id, "type": "Ingestion"}), mimetype='application/json',
                        status=202)


# Recursive model spec broken, is fixed in next version of flask_restx, but no idea when it will release
# Sources:
# - https://github.com/python-restx/flask-restx/pull/174
# - https://github.com/python-restx/flask-restx/issues/211
TaskStatusModel = api.model("TaskStatus", {
    'name': fields.String,
    'args': fields.String,
    'status': fields.String,
    'id': fields.String
})
TaskStatusModel["children"] = fields.List(fields.Nested(TaskStatusModel), default=[])
TaskStatusModel["parent"] = fields.Nested(TaskStatusModel)


@api.route('/task-status')
@api.doc(description="Checks the status of a task.")
@api.doc(params={
    'task_id': {'description': 'ID of task to check', 'in': 'query', 'type': 'string', 'required': 'true'},
})
class TaskStatus(Resource):
    @api.response(200, 'Success', TaskStatusModel)
    @api.response(400, 'Missing task id query parameter')
    @api.response(404, 'Task does not exist')
    @api.response(500, 'Task could not be loaded from backend')
    def get(self):
        task_id = request.args.get("task_id")
        if not task_id:
            return Response("Missing task id query parameter", status=400)

        result_tuple = db.get_celery_task(task_id)
        if not result_tuple:
            return Response("Task does not exist", status=404)

        result = result_from_tuple(result_tuple)
        if result is None:
            return Response("Task could not be loaded from backend", status=500)

        return Response(json.dumps(generate_status_tree(result)), mimetype='application/json', status=200)


@api.route('/purge')
@api.doc(description="Purges all of the databases.")
class Purge(Resource):
    @api.response(200, 'Success')
    def get(self):
        db.purge()
        discovery.crud.delete_all_nodes()
        return Response('Success', status=200)


# TODO: Implement the algorithm for PK-FK and remove metanome
@api.route('/profile-metanome')
@api.doc(
    description="Runs Metanome profiling for all tables, which is used to obtain KFK relations between the tables.")
@api.doc(params={
    'bucket': {'description': 'Path to the S3 bucket with data', 'in': 'query', 'type': 'string', 'required': 'true',
               'default': 'data'},
})
class ProfileMetanome(Resource):
    @api.response(200, 'Success')
    @api.response(400, 'Missing bucket query parameter')
    @api.response(404, 'Bucket does not exist')
    @api.response(500, 'Cannot connect to Metanome')
    def get(self):
        bucket = request.args.get('bucket')

        if not bucket:
            return Response('Missing bucket query parameter', status=400)

        if not search.io_tools.bucket_exists(bucket):
            return Response("Bucket does not exist", 404)

        try:
            profile_metanome(bucket)
            return Response('Success', status=200)
        except ConnectionError:
            return Response('Cannot connect to Metanome', status=500)


@api.route('/filter-connections')
@api.doc(description="Filters spurious connections. This step is required after the ingestion phase.")
class FilterConnections(Resource):
    # NOTE: We can only have a single response per code, see: https://github.com/python-restx/flask-restx/issues/274
    @api.response(200, 'Success', api.model('DeletedRelations', {
        'deleted_relations': fields.List(fields.String)
    }))
    def get(self):
        deleted_relations = delete_spurious_connections()

        if not deleted_relations:
            logging.info("No relations have been deleted")

        return Response(json.dumps({"deleted_relations": deleted_relations}), mimetype='application/json', status=200)


@api.route('/profile-valentine')
@api.doc(
    description="Runs Valentine profiling between the given table and all other ingested tables, used for finding columns that are related.")
@api.doc(params={
    'bucket': {'description': 'Path to the S3 bucket where the table resides', 'in': 'query', 'type': 'string',
               'required': 'true', 'default': 'data'},
    'table_path': {'description': 'Path to the table', 'in': 'query', 'type': 'string', 'required': 'true'}
})
class ProfileValentine(Resource):
    @api.response(202, 'Success', TaskIdModel)
    @api.response(400, 'Missing table path or bucket query parameters')
    @api.response(403, 'Table has not been ingested yet')
    @api.response(404, 'Bucket or table does not exist')
    def get(self):
        bucket = request.args.get('bucket')
        table_path = request.args.get('table_path')

        if not bucket or not table_path:
            return Response("Missing table path or bucket query parameters", status=400)

        if not search.io_tools.bucket_exists(bucket):
            return Response("Bucket does not exist", status=404)

        if not search.io_tools.table_exists(bucket, table_path):
            return Response("Table does not exist", status=404)

        if not db.table_exists(table_path):
            return Response("Table has not been ingested yet", status=403)

        task = profile_valentine_star.delay(bucket, table_path)
        db.save_celery_task(task.id, task.as_tuple())

        return Response(json.dumps({"task_id": task.id, "type": "Valentine Profiling"}), mimetype='application/json',
                        status=202)


@api.route('/add-table')
@api.doc(description="Initiates ingestion and profiling for the table at the given path.")
@api.doc(params={
    'bucket': {'description': 'Path to the S3 bucket where the table resides', 'in': 'query', 'type': 'string',
               'required': 'true', 'default': 'data'},
    'table_path': {'description': 'Path to the table', 'in': 'query', 'type': 'string', 'required': 'true'}
})
class AddTable(Resource):
    @api.response(400, 'Missing table path or bucket query parameters')
    @api.response(204, 'Table was already processed')
    @api.response(404, 'Bucket or table does not exist')
    def get(self):
        bucket = request.args.get('bucket')
        table_path = request.args.get('table_path')

        if not bucket or not table_path:
            return Response("Missing table path or bucket query parameters", status=400)

        if not search.io_tools.bucket_exists(bucket):
            return Response("Bucket does not exist", status=404)

        if not search.io_tools.table_exists(bucket, table_path):
            return Response("Table does not exist", status=404)

        if db.get_table(table_path):
            return Response("Table was already processed", status=204)

        task = (add_table.si(bucket, table_path) | profile_valentine_star.si(bucket, table_path)).apply_async()
        db.save_celery_task(task.id, task.as_tuple())

        return Response(json.dumps({"task_id": task.id, "type": "Single Ingestion"}), mimetype='application/json',
                        status=202)


@api.route('/get-table-csv')
@api.doc(description="Gets a part of the table at the given path as CSV.")
@api.doc(params={
    'bucket': {'description': 'Path to the S3 bucket where the table resides', 'in': 'query', 'type': 'string',
               'required': 'true', 'default': 'data'},
    'table_path': {'description': 'Path to the table', 'in': 'query', 'type': 'string', 'required': 'true'},
    'rows': {'description': 'Number of rows to get from the top', 'in': 'query', 'type': 'string', 'required': 'true'}
})
class GetTableCSV(Resource):
    @api.response(400, 'Missing table path, rows or bucket query parameters')
    @api.response(404, 'Bucket or table does not exist')
    def get(self):
        bucket = request.args.get('bucket')
        table_path = request.args.get('table_path')
        rows = request.args.get('rows', type=int)
        if not bucket or not table_path or not rows:
            return Response('Missing table path, rows or bucket query parameters', status=400)

        if not search.io_tools.bucket_exists(bucket):
            return Response("Bucket does not exist", status=404)

        if not search.io_tools.table_exists(bucket, table_path):
            return Response("Table does not exist", status=404)

        return search.io_tools.get_ddf(bucket, table_path).head(rows).to_csv()


RelatedTableModel = api.model("RelatedTable", {
    'links': fields.List(fields.String),
    'explanation': fields.String
})


@api.route('/get-related')
@api.doc(description="Get all the assets on the path connecting the source and the target tables.")
@api.doc(params={
    'source_table_path': {'description': 'Path to the source table', 'in': 'query', 'type': 'string',
                          'required': 'true'},
    'target_table_path': {'description': 'Path to the target table', 'in': 'query', 'type': 'string',
                          'required': 'true'}
})
class GetRelatedNodes(Resource):
    @api.response(200, 'Success',
                  api.model("RelatedTables", {"RelatedTables": fields.List(fields.Nested(RelatedTableModel))}))
    @api.response(400, 'Missing table paths query parameters')
    @api.response(403, 'Source and target are the same')
    @api.response(404, 'Table does not exist')
    def get(self):
        from_table = request.args.get("source_table_path")
        to_table = request.args.get("target_table_path")

        if not from_table or not to_table:
            return Response("Please provide both a source table path and a target table path as query parameters",
                            status=400)

        if from_table == to_table:
            return Response("Source and target table paths should be different", status=403)

        node = discovery.queries.get_node_by_prop(source_name=from_table)
        if len(node) == 0:
            return Response("Table does not exist", status=404)

        node = discovery.queries.get_node_by_prop(source_name=to_table)
        if len(node) == 0:
            return Response("Table does not exist", status=404)

        paths = get_related_between_two_tables(from_table, to_table)
        return Response(json.dumps({"RelatedTables": paths}), mimetype='application/json', status=200)


# Apparently we need to make models for every nested field...
# See: https://github.com/noirbizarre/flask-restplus/issues/292 (bug still exists in flask_restx)
MatchModel = api.model("Match", {
    'PK': fields.Nested(api.model("Relation", {"from_id": fields.String, "to_id": fields.String})),
    'RELATED': fields.Nested(api.model("Profiles", {"coma": fields.Float})),
    'explanation': fields.String
})
JoinableTableModel = api.model("JoinableTable", {
    'matches': fields.List(fields.Nested(MatchModel)),
    'table_name': fields.String
})


@api.route('/get-joinable')
@api.doc(description="Gets all assets that are joinable with the given source table.")
@api.doc(params={
    'table_path': {'description': 'Path to the table', 'in': 'query', 'type': 'string', 'required': 'true'}
})
class GetJoinable(Resource):
    @api.response(200, 'Success', api.model("JoinableTables", {
        "JoinableTables": fields.List(fields.Nested(JoinableTableModel))}))  # TODO: specify return model
    @api.response(400, 'Missing table paths query parameters')
    @api.response(404, 'Table does not exist')
    def get(self):
        args = request.args
        table_path = args.get("table_path")
        if table_path is None:
            return Response("Please provide a table path as query parameter", status=400)

        node = discovery.queries.get_node_by_prop(source_name=table_path)
        if len(node) == 0:
            return Response("Table does not exist", status=404)

        return Response(json.dumps({"JoinableTables": discovery.queries.get_joinable(table_path)}),
                        mimetype='application/json', status=200)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=443)
