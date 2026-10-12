import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def api(path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode("utf-8"))


s = api("/api/sessions", {"source": "web"})
sid = s["session_id"]
print("OPENING:", s["reply"])
print("stage:", s["stage"], "lang:", s["language"])

turns = [
    "Sí, empecemos",
    "Me llamo Laura Gómez",
    "sí, tengo licencia vigente",
    "vivo en Valencia, barrio de Ruzafa",
    "tengo disponibilidad completa",
    "por la mañana",
    "sí, trabajé 2 años en Glovo",
    "puedo empezar el 2026-11-02",
    "¿el pago es por entrega o por hora?",
    "sí, confirmo",
]
for t in turns:
    r = api(f"/api/sessions/{sid}/messages", {"text": t})
    print(f"\nUSER: {t}")
    print("AGENT:", r["reply"])
    print("stage:", r["stage"], "status:", r["status"], "adv:", r["advanced"], "rag:", r["rag_used"])
    if r["summary"]:
        print("SUMMARY:", json.dumps(r["summary"], ensure_ascii=False)[:600])

sess = api(f"/api/sessions/{sid}")
print("\nmessages stored:", len(sess["messages"]), "events:", len(sess["events"]))
print("flags:", sess["flags"])
print("recruiter_note:", (sess["summary"] or {}).get("recruiter_notes"))
print("data:", json.dumps(sess["data"], ensure_ascii=False))

an = api("/api/analytics")
print("\nANALYTICS:", json.dumps(an, ensure_ascii=False)[:500])
