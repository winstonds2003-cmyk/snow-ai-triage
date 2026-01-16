from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
from requests.auth import HTTPBasicAuth
import json
import os
from openai import OpenAI

# -------------------------
# ServiceNow Config (use ENV vars for security)
# -------------------------
INSTANCE = os.getenv("SN_INSTANCE", "https://dev195416.service-now.com")
SN_USER = os.getenv("SN_USER", "langgraph.bot")
SN_PASSWORD = os.getenv("SN_PASSWORD")  # set this in Render env vars

if not SN_PASSWORD:
    # Local fallback if you REALLY want it.
    # Better: keep password out of code.
    SN_PASSWORD = ""

# -------------------------
# OpenAI Config
# -------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()


# ✅ Payload coming from Swagger OR ServiceNow
class IncidentPayload(BaseModel):
    number: str
    short_description: str = ""
    description: str = ""


# -------------------------
# Helper: Get sys_id using INC number
# -------------------------
def get_sys_id_from_number(inc_number: str) -> str:
    url = f"{INSTANCE}/api/now/table/incident"
    params = {
        "sysparm_query": f"number={inc_number}",
        "sysparm_fields": "sys_id"
    }

    r = requests.get(
        url,
        auth=HTTPBasicAuth(SN_USER, SN_PASSWORD),
        headers={"Accept": "application/json"},
        params=params
    )

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()
    result = data.get("result", [])

    if not result:
        raise HTTPException(status_code=404, detail=f"Incident {inc_number} not found in ServiceNow.")

    return result[0]["sys_id"]


# -------------------------
# Helper: Find user sys_id by username (ex: winston.dsouza)
# -------------------------
def get_user_sys_id(username: str) -> str:
    url = f"{INSTANCE}/api/now/table/sys_user"
    params = {
        "sysparm_query": f"user_name={username}",
        "sysparm_fields": "sys_id"
    }

    r = requests.get(
        url,
        auth=HTTPBasicAuth(SN_USER, SN_PASSWORD),
        headers={"Accept": "application/json"},
        params=params
    )

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()
    result = data.get("result", [])

    if not result:
        raise HTTPException(status_code=404, detail=f"User {username} not found in ServiceNow.")

    return result[0]["sys_id"]


# -------------------------
# Helper: Find assignment group sys_id by name (ex: Policy_Admin_Triage)
# -------------------------
def get_group_sys_id(group_name: str) -> str:
    url = f"{INSTANCE}/api/now/table/sys_user_group"
    params = {
        "sysparm_query": f"name={group_name}",
        "sysparm_fields": "sys_id"
    }

    r = requests.get(
        url,
        auth=HTTPBasicAuth(SN_USER, SN_PASSWORD),
        headers={"Accept": "application/json"},
        params=params
    )

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()
    result = data.get("result", [])

    if not result:
        raise HTTPException(status_code=404, detail=f"Group {group_name} not found in ServiceNow.")

    return result[0]["sys_id"]


# -------------------------
# AI Decision: Routing logic (simple for demo)
# -------------------------
def decide_routing(short_desc: str, desc: str):
    text = (short_desc + " " + desc).lower()

    # ✅ Demo logic: if endorsement OR roadside -> assign to Winston
    if "endorsement" in text or "roadside" in text:
        return {
            "group_name": "Policy_Admin_Triage",
            "assignee_username": "winston.dsouza"
        }

    # Default
    return {
        "group_name": "Policy_Admin_Triage",
        "assignee_username": ""
    }


# -------------------------
# Helper: Update Incident work notes + routing
# -------------------------
def update_incident(sys_id: str, work_notes: str, group_sys_id: str = "", user_sys_id: str = ""):
    sys_id = sys_id.lower().strip()

    url = f"{INSTANCE}/api/now/table/incident/{sys_id}"

    payload = {
        "work_notes": work_notes,
        "state": "2"  # In Progress
    }

    if group_sys_id:
        payload["assignment_group"] = group_sys_id
    if user_sys_id:
        payload["assigned_to"] = user_sys_id

    r = requests.patch(
        url,
        auth=HTTPBasicAuth(SN_USER, SN_PASSWORD),
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload)
    )

    if r.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Incident sys_id={sys_id} not found in ServiceNow.")

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    return r.json()


# -------------------------
# ✅ OpenAI LLM Triage Report
# -------------------------
def triage_report(inc_number: str, short_desc: str, desc: str) -> str:
    prompt = f"""
You are an Incident Triage Assistant for insurance policy administration systems.

Your goal:
- Identify probable root cause
- Provide next best actions (3-6 bullets)
- Provide confidence score between 0.60 and 0.95

Incident Details:
Incident Number: {inc_number}
Short Description: {short_desc}
Description: {desc}

Return ONLY in this format:

✅ OpenAI Triage Summary
Incident: <INC>
Short desc: <short desc>

Probable root cause:
- <1 line cause>

Next actions:
- <action 1>
- <action 2>
- <action 3>

Confidence: <decimal>
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful incident triage assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2
    )

    return response.choices[0].message.content.strip()


# -------------------------
# API Endpoint
# -------------------------
@app.post("/triage")
def triage_incident(payload: IncidentPayload):
    """
    1) Find sys_id using incident number
    2) Generate triage report using OpenAI
    3) Decide routing based on keywords
    4) Update ServiceNow work notes + state + assignment group + assigned_to
    """

    if not SN_PASSWORD:
        raise HTTPException(status_code=500, detail="SN_PASSWORD env var is missing.")

    # 1) Get incident sys_id
    inc_sys_id = get_sys_id_from_number(payload.number)

    # 2) Generate LLM triage notes
    notes = triage_report(payload.number, payload.short_description, payload.description)

    # 3) Decide routing (demo logic)
    routing = decide_routing(payload.short_description, payload.description)

    group_sys_id = ""
    user_sys_id = ""

    if routing.get("group_name"):
        group_sys_id = get_group_sys_id(routing["group_name"])

    if routing.get("assignee_username"):
        user_sys_id = get_user_sys_id(routing["assignee_username"])

    # Add routing info inside notes
    if routing.get("assignee_username"):
        notes += f"\n\n✅ Auto-routing:\n- Assignment group: {routing['group_name']}\n- Assigned to: {routing['assignee_username']}"

    # 4) Update incident in ServiceNow
    update_incident(inc_sys_id, notes, group_sys_id, user_sys_id)

    return {
        "status": "updated",
        "incident": payload.number,
        "sys_id": inc_sys_id,
        "assignment_group": routing.get("group_name", ""),
        "assigned_to": routing.get("assignee_username", ""),
        "notes": notes
    }
