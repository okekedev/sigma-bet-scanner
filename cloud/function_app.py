"""Sigma-bet scanner — Azure Functions (Python v2 model).

Timers (UTC; ET = UTC-4 in summer):
  scan_cycle : every 2h during market hours (13:45, 15:45, 17:45, 19:45 UTC Mon-Fri
               = 9:45a, 11:45a, 1:45p, 3:45p ET)
  eod_update : 09:00 UTC Tue-Sat (5:00a ET) — flat files publish overnight

HTTP:
  GET /api/run?job=scan|eod  (function key) — manual kick for testing
"""
import logging
import azure.functions as func
from scanner import core

app = func.FunctionApp()

@app.timer_trigger(schedule="0 45 13,15,17,19 * * 1-5", arg_name="timer", run_on_startup=False)
def scan_cycle(timer: func.TimerRequest) -> None:
    result = core.run_scan()
    logging.info("scan_cycle: %s", result)

@app.timer_trigger(schedule="0 0 9 * * 2-6", arg_name="timer", run_on_startup=False)
def eod_update(timer: func.TimerRequest) -> None:
    result = core.run_eod()
    logging.info("eod_update: %s", result)

@app.route(route="run", auth_level=func.AuthLevel.FUNCTION)
def run_manual(req: func.HttpRequest) -> func.HttpResponse:
    job = req.params.get("job", "scan")
    result = core.run_eod() if job == "eod" else core.run_scan()
    return func.HttpResponse(result, status_code=200)
