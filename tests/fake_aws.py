"""Stand-ins for boto3 so the receipt code can be tested on a laptop with no AWS account.

    import fake_aws; fake_aws.install()      # before importing lambda_function
"""
import os
import sys
import types


class ClientError(Exception):
    def __init__(self, response, operation_name="op"):
        super().__init__(response.get("Error", {}).get("Message", ""))
        self.response = response
        self.operation_name = operation_name


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None, **kw):
        key = Item["receipt_id"]
        if ConditionExpression and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}})
        self.items[key] = dict(Item)

    def get_item(self, Key, **kw):
        item = self.items.get(Key["receipt_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kw):
        items = [dict(i) for i in self.items.values()]
        prefix = kw.get("ExpressionAttributeValues", {}).get(":p")
        if prefix is not None:  # supports the begins_with(s3_key, :p) filter the website uses
            items = [i for i in items if str(i.get("s3_key", "")).startswith(prefix)]
        return {"Items": items}


class TypeSerializer:
    def serialize(self, v):
        from decimal import Decimal
        if v is None:
            return {"NULL": True}
        if isinstance(v, bool):
            return {"BOOL": v}
        if isinstance(v, (int, Decimal)):
            return {"N": str(v)}
        if isinstance(v, str):
            return {"S": v}
        if isinstance(v, dict):
            return {"M": {k: self.serialize(x) for k, x in v.items()}}
        if isinstance(v, (list, tuple)):
            return {"L": [self.serialize(x) for x in v]}
        raise TypeError(f"Unsupported type {type(v)}")  # floats are rejected, like the real one


class TypeDeserializer:
    def deserialize(self, v):
        from decimal import Decimal
        (kind, x), = v.items()
        return {"S": lambda: x, "N": lambda: Decimal(x), "BOOL": lambda: x, "NULL": lambda: None,
                "M": lambda: {k: self.deserialize(y) for k, y in x.items()},
                "L": lambda: [self.deserialize(y) for y in x]}[kind]()


def _dynamodb_client(client):
    ser, de = TypeSerializer(), TypeDeserializer()

    def put_item(TableName, Item, ConditionExpression=None):
        TABLE.put_item(Item={k: de.deserialize(v) for k, v in Item.items()}, ConditionExpression=ConditionExpression)
        return {}

    def get_item(TableName, Key, **kw):
        found = TABLE.get_item(Key={k: de.deserialize(v) for k, v in Key.items()}).get("Item")
        return {"Item": {k: ser.serialize(v) for k, v in found.items()}} if found else {}

    client.handlers.update(put_item=put_item, get_item=get_item)
    return client


class FakeClient:
    """Records calls; behaviour for detect_text / get_object is plugged in by tests."""

    def __init__(self, name):
        self.name = name
        self.calls = []
        self.handlers = {}

    def __getattr__(self, op):
        def call(*args, **kwargs):
            self.calls.append((op, kwargs))
            if op in self.handlers:
                return self.handlers[op](*args, **kwargs)
            return {}
        return call


CLIENTS = {}
TABLE = FakeTable()


def client(name, *a, **kw):
    if name not in CLIENTS:
        CLIENTS[name] = _dynamodb_client(FakeClient(name)) if name == "dynamodb" else FakeClient(name)
    return CLIENTS[name]


def resource(name, *a, **kw):
    return types.SimpleNamespace(Table=lambda table_name: TABLE)


def install():
    os.environ.setdefault("TABLE_NAME", "test-table")
    os.environ.setdefault("TOPIC_ARN", "arn:aws:sns:ap-southeast-2:000000000000:test")
    boto3 = types.ModuleType("boto3")
    boto3.client, boto3.resource = client, resource
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kw: None
    botocore.exceptions, botocore.config = exceptions, config
    boto3.__path__ = []  # a package, so "boto3.dynamodb.types" can be imported
    dynamodb = types.ModuleType("boto3.dynamodb")
    dtypes = types.ModuleType("boto3.dynamodb.types")
    dtypes.TypeSerializer, dtypes.TypeDeserializer = TypeSerializer, TypeDeserializer
    dynamodb.__path__, dynamodb.types, boto3.dynamodb = [], dtypes, dynamodb
    sys.modules.update({"boto3": boto3, "boto3.dynamodb": dynamodb, "boto3.dynamodb.types": dtypes,
                        "botocore": botocore, "botocore.exceptions": exceptions, "botocore.config": config})
