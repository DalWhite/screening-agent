# Abandoned by silence, then resumed within 48h — re-asks the exact dropped question

_Session e8a309ed-79c3-48b5-a5a9-848418a5fdfc · language es · today 2026-10-11_

**Marisol:** ¡Hola! Soy Marisol, del equipo de Grupo Sazón. Te hago 7 preguntas rápidas, unos 4 minutos. ¿Empezamos?

**Candidate:** hola

**Marisol:** ¿Cuál es tu nombre completo?

_Stage: intro → name_

**Candidate:** Lucía Ferrer

**Marisol:** ¿Tienes licencia de conducir vigente? Sí o no.

_Stage: name → license_

**Candidate:** sí, licencia en regla

**Marisol:** ¿En qué ciudad y barrio repartirías?

_Stage: license → location_

**Candidate:** Valencia, El Carmen

**Marisol:** ¿Qué disponibilidad tienes: jornada completa, medio tiempo o solo fines de semana?

_Stage: location → availability_

**Candidate:** *(goes silent — session auto-closes as abandoned)*

**Marisol:** Terminamos. Un reclutador revisa tu caso y te contacta en 24-48h.

_Candidate returns 2 hours later and reopens the conversation_


**Marisol:** ¡Bienvenido de nuevo! Retomamos justo donde lo dejaste. ¿Qué disponibilidad tienes: jornada completa, medio tiempo o solo fines de semana?

_Stage: abandoned → availability (re-asked)_

**Candidate:** solo fines de semana

**Marisol:** ¿Qué turno prefieres: mañana, tarde, noche o flexible?

_Stage: availability → schedule_

**Candidate:** flexible

**Marisol:** ¿Has repartido antes? ¿Cuántos años y en qué plataformas (Glovo, Uber Eats, Deliveroo...)?

_Stage: schedule → experience_

**Candidate:** no, no tengo experiencia

**Marisol:** ¿Desde qué fecha podrías empezar?

_Stage: experience → start_date_

**Candidate:** empezaría el 2026-11-10

**Marisol:** Encajas para el puesto. Un reclutador te llamará en 24-48h para la entrevista de 15 minutos. ¡Gracias por tu tiempo!

_Stage: start_date → qualified_

---

**Final stage:** qualified · **flags:** —

```json
{
  "qualified": true,
  "outcome": "qualified",
  "disqualify_reason": null,
  "completed_stages": [
    "intro",
    "name",
    "license",
    "location",
    "availability",
    "schedule",
    "experience",
    "start_date",
    "review"
  ],
  "dropped_at_stage": null,
  "duration_seconds": 0,
  "candidate_turns": 8,
  "languages": [
    "es"
  ],
  "data": {
    "full_name": "Lucía Ferrer",
    "has_license": true,
    "city": "Valencia",
    "zone": "El Carmen",
    "country": "ES",
    "in_service_area": true,
    "availability": [
      "weekends"
    ],
    "preferred_schedule": "flexible",
    "experience_years": 0,
    "experience_platforms": [],
    "start_date": "2026-11-10"
  },
  "recruiter_notes": [
    "Lucía Ferrer — Valencia (El Carmen), ES — fines de semana — horario flexible — 0 años de experiencia — disponible desde 2026-11-10 — licencia de conducir vigente"
  ],
  "flags": [],
  "generated_at": "2026-10-15T13:30:22.931540Z"
}
```
