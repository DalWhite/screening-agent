# Qualified candidate (ES) — asks a pay question answered from the FAQ

_Profile delivery_courier (Grupo Sazón) · session e8a49df0-0743-4ee9-b509-ac47f9e373c7 · language es · today 2026-10-11_

**Marisol:** ¡Hola! Soy Marisol, del equipo de Grupo Sazón. Te hago 7 preguntas rápidas, unos 4 minutos. ¿Empezamos?

**Candidate:** Hola, quiero aplicar para repartidor

**Marisol:** ¿Cuál es tu nombre completo?

_Stage: intro → name_

**Candidate:** Laura Jiménez Ortiz

**Marisol:** ¿Tienes licencia de conducir vigente? Sí o no.

_Stage: name → license_

**Candidate:** ¿Cuánto pagan por hora?

**Marisol:** El pago es por entrega y por hora mínima garantizada: 8,50 EUR/hora garantizados en España más 3,20-4,10 EUR por entrega según zona y distancia. En México: 55-70 MXN por entrega con garantía de 120 MXN/hora en horas pico.

¿Tienes licencia de conducir vigente? Sí o no.

_RAG: answered from FAQ pay_es_

**Candidate:** sí, tengo carnet

**Marisol:** ¿En qué ciudad y barrio repartirías?

_Stage: license → location_

**Candidate:** Madrid, Malasaña

**Marisol:** ¿Qué disponibilidad tienes: jornada completa, medio tiempo o solo fines de semana?

_Stage: location → availability_

**Candidate:** jornada completa

**Marisol:** ¿Qué turno prefieres: mañana, tarde, noche o flexible?

_Stage: availability → schedule_

**Candidate:** mañana

**Marisol:** ¿Has repartido antes? ¿Cuántos años y en qué plataformas (Glovo, Uber Eats, Deliveroo...)?

_Stage: schedule → experience_

**Candidate:** 2 años en Glovo

**Marisol:** ¿Desde qué fecha podrías empezar?

_Stage: experience → start_date_

**Candidate:** Puedo empezar el 2026-11-02. Mi teléfono es 600112233

**Marisol:** Encajas para el puesto. Un reclutador te llamará en 24-48h para la entrevista de 15 minutos. ¡Gracias por tu tiempo!

_Stage: start_date → qualified_

_Async check license_check: error (No se recibió ningún número de documento)_

---

**Final stage:** qualified · **flags:** ['check_error:license_check']

```json
{
  "profile_id": "delivery_courier",
  "qualified": true,
  "outcome": "qualified",
  "disqualify_reason": null,
  "close_reason": null,
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
  "candidate_turns": 9,
  "languages": [
    "es"
  ],
  "data": {
    "full_name": "Laura Jiménez Ortiz",
    "has_license": true,
    "city": "Madrid",
    "zone": "Malasaña",
    "country": "ES",
    "in_service_area": true,
    "availability": [
      "full_time"
    ],
    "preferred_schedule": "morning",
    "experience_years": 2,
    "experience_platforms": [
      "glovo"
    ],
    "start_date": "2026-11-02",
    "license_check": "pending"
  },
  "score": 80.0,
  "recruiter_notes": [
    "Laura Jiménez Ortiz — Licencia de conducir ✓ — Madrid (Malasaña), ES — Jornada completa — Mañana — 2 años (glovo) — desde 2026-11-02"
  ],
  "flags": [],
  "generated_at": "2026-11-04T14:18:53.309258Z"
}
```
