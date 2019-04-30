import logging

import azure.functions as func
import requests
import json
import urllib
import os
import pymongo

logger = logging.getLogger('Azure Deployment Approvals')

SLACK_WEB_HOOK = os.environ['SLACK_WEB_HOOK']
MONGO_DB_CONNECTION = os.environ['MONGO_DB_CONNECTION']

mongo_client = pymongo.MongoClient(MONGO_DB_CONNECTION)
mongo_database = mongo_client.customproviders

approvals = mongo_database.approvals

# Sends an external notification (This can be to Slack, Teams, ect..)
def send_external_notification(tracking_id):
    deployment_notification = {
        "blocks" : [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "an Azure deployment has been triggered"
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Details"
                    },
                    "url": "https://portal.azure.com",
                    "action_id": "azureportal"
                }
            },
            {
                "type": "actions",
                "block_id": f"{tracking_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve"
                        },
                        "value": "approve",
                        "action_id": "approve"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Reject"
                        },
                        "value": "reject",
                        "action_id": "reject"
                    }
                ]
            }
        ]
    }
    
    result = requests.post(SLACK_WEB_HOOK, json=deployment_notification)

    logger.info(f"Slack Result: {result}")

# Recieves the result of an incoming slack interaction
def post(req_body, requestPath, req_url) -> func.HttpResponse:
    requestValue = req_body["actions"][0]["value"]
    approval = approvals.find_one({ "_id" : requestPath })

    if (requestValue == "approve"):
        approval["data"]["properties"]["provisioningState"] = "Succeeded"
    else:
        approval["data"]["properties"]["provisioningState"] = "Failed"

    # Attempt to Update the Approval in Storage. However, it may have been deleted.
    try:
        approvals.replace_one(
            { "_id": approval["_id"] },
            approval)
    except:
        return func.HttpResponse(
            json.dumps({}),
            headers={ "Content-Type": "application/json" },
            status_code=404
        )

    return func.HttpResponse(
        json.dumps({}),
        headers={ "Content-Type": "application/json" },
        status_code=200
    )

# Tracks the long running operation of approval creation -> approved
def get(req_body, requestPath, req_url) -> func.HttpResponse:
    approval = approvals.find_one({"_id" : requestPath })

    if (approval is not None):
        if (approval["data"]["properties"]["provisioningState"] == "Accepted"):
            return func.HttpResponse(
                json.dumps({}),
                headers={ "Content-Type": "application/json", "retry-after": "30"},
                status_code=202
            )
        else:
            return func.HttpResponse(
                json.dumps(approval["data"]),
                headers={ "Content-Type": "application/json" },
                status_code=200
            )

    return func.HttpResponse(
        json.dumps({ "error" : { "code" : "InvalidLocation", "message": "Invalid Location Header!"}}),
        headers={ "Content-Type": "application/json" },
        status_code=400
    )

# Creates a new approval request.
def put(req_body, requestPath, req_url) -> func.HttpResponse:
    req_body["properties"]["provisioningState"] = "Accepted"

    approvals.replace_one(
        { "_id" : requestPath},
        { "_id" : requestPath, "data": req_body },
        upsert = True)

    # Send external message with tracking id, so we know on callback how to look it up.
    send_external_notification(requestPath)

    return func.HttpResponse(
        json.dumps(req_body),
        headers={ "Content-Type": "application/json", "location": f"{req_url}&location={requestPath}", "retry-after": "30"},
        status_code=202
    )

# Deletes an existing approval
def delete(req_body, requestPath, req_url) -> func.HttpResponse:
    approval = approvals.find_one_and_delete({ "_id" : requestPath })

    return func.HttpResponse(
        json.dumps({}),
        headers={ "Content-Type": "application/json" },
        status_code=200 if approval is not None else 204
    )

# Dictionary for supported methods.
method_dictionary = {
    "GET": get,
    "POST": post,
    "PUT": put,
    "DELETE": delete
}

# Retrieves the request body
def get_requestBody(req: func.HttpRequest):
    if req.method == "PUT":
        return req.get_json()
    elif req.method == "POST":
        return json.loads(urllib.parse.unquote(req.get_body().decode("utf-8").replace("payload=", "")))
    else:
        return {}

# Retrieves the request path
def parse_requestPath(req: func.HttpRequest, req_body):
    if req.method == "GET":
        return req.params["location"]
    elif req.method == "POST":
        return req_body["actions"][0]["block_id"]
    else:
        return req.headers.get('x-ms-customproviders-requestpath')


async def main(req: func.HttpRequest) -> func.HttpResponse:
    req_body = get_requestBody(req)
    requestPath =  parse_requestPath(req, req_body)

    logger.info(f'Http Incoming Request From: Url=[{req.url}] Params=[{req.params}] Body=[{req_body}] With: {requestPath}')
    
    if req.method in method_dictionary:
        return method_dictionary[req.method](req_body, requestPath, req.url)

    return func.HttpResponse(
        json.dumps({ "error" : {  "code" : "MethodNotSupported", "message": "The requested method is not supported."}}),
        headers={ "Content-Type": "application/json" },
        status_code=404
    )

