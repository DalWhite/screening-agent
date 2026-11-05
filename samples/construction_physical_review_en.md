# Construction labourer (EN) — reports physical limits: qualified but routed to recruiter review

_Profile construction_laborer (Constructora Horizonte) · session 7f4fd08b-81ca-430f-9614-ae0f83e8564c · language en · today 2026-10-11_

**Andrés:** Hi! I'm Andrés from Constructora Horizonte. We're hiring construction labourers, no experience needed. 10 short questions, about 3 minutes. Shall we start?

**Candidate:** Hi, I want to apply

**Andrés:** What's your name? First and last name.

_Stage: intro → name_

**Candidate:** John Smith

**Andrés:** Are you 18 or older?

_Stage: name → adult_

**Candidate:** yes

**Andrés:** Are you allowed to work in Spain (Spanish/EU ID, or NIE with work permit)? Yes or no.

_Stage: adult → work_permit_

**Candidate:** yes

**Andrés:** To register you we need to verify your ID. What's your DNI, NIE or passport number? It's only used for that check.

_Stage: work_permit → id_document_

**Candidate:** X1234567L

**Andrés:** Which city or area do you live in? We have sites in Madrid, Toledo, Valencia, Seville, Malaga and Zaragoza.

_Stage: id_document → location_

**Candidate:** Malaga

**Andrés:** The job is physical: carrying materials up to 25 kg and standing most of the shift. Is that OK for you?

_Stage: location → physical_

**Candidate:** no, I can't lift heavy loads

**Andrés:** Do you have the 20-hour construction safety course (PRL)? If not, no problem: we provide it.

_Stage: physical → prl_

**Candidate:** yes I have it

**Andrés:** Have you worked in construction or a trade before (bricklaying, painting, formwork...)? How many years? If not, say 0.

_Stage: prl → experience_

**Candidate:** 5 years as a bricklayer

**Andrés:** Sites start at 7:00 and move around. How would you get there: your own vehicle or public transport?

_Stage: experience → transport_

**Candidate:** I drive my own car

**Andrés:** Last one: from which day could you start?

_Stage: transport → start_date_

**Candidate:** next week

**Andrés:** Thanks, that's everything! I'm verifying your details and will let you know here as soon as I hear back.

_Stage: start_date → verifying_

_Async check id_check: valid (Documento verificado en el registro)_

**Andrés:** Great, you meet the requirements! A site manager will call you within 24-48h to agree the site and your start day. Thanks!

---

**Final stage:** qualified · **flags:** ['physical_review']

```json
{
  "profile_id": "construction_laborer",
  "qualified": true,
  "outcome": "qualified",
  "disqualify_reason": null,
  "close_reason": null,
  "completed_stages": [
    "intro",
    "name",
    "adult",
    "work_permit",
    "id_document",
    "location",
    "physical",
    "prl",
    "experience",
    "transport",
    "start_date",
    "review"
  ],
  "dropped_at_stage": null,
  "duration_seconds": 0,
  "candidate_turns": 11,
  "languages": [
    "en"
  ],
  "data": {
    "full_name": "John Smith",
    "is_adult": true,
    "work_permit": true,
    "id_document": "•••••567L",
    "id_document_type": "nie",
    "city": "Málaga",
    "zone": null,
    "country": "ES",
    "in_service_area": true,
    "physical_ok": false,
    "has_prl_course": true,
    "construction_experience_years": 5,
    "construction_trades": [
      "albañilería"
    ],
    "transport": "own_vehicle",
    "start_date": "2026-10-18",
    "id_check": "valid"
  },
  "score": 80.0,
  "recruiter_notes": [
    "El candidato indica que el trabajo físico pesado puede ser un problema: hablar con él sobre tareas adecuadas (no es un descarte automático).",
    "John Smith — Aged 18+ ✓ — Right to work ✓ — NIE •••••567L — Málaga, ES — Physical work ✗ — 20h safety course (PRL) ✓ — 5 years (albañilería) — Own vehicle — from 2026-10-18"
  ],
  "flags": [
    "physical_review"
  ],
  "generated_at": "2026-11-04T14:18:53.570271Z"
}
```
