# Disqualified — city outside service areas after one retry (waitlist)

_Profile delivery_courier (Grupo Sazón) · session b02a0365-97d6-4996-bb9a-c838f5ebe0f8 · language es · today 2026-10-11_

**Marisol:** ¡Hola! Soy Marisol, del equipo de Grupo Sazón. Te hago 7 preguntas rápidas, unos 4 minutos. ¿Empezamos?

**Candidate:** hola

**Marisol:** ¿Cuál es tu nombre completo?

_Stage: intro → name_

**Candidate:** Ana Ruiz

**Marisol:** ¿Tienes licencia de conducir vigente? Sí o no.

_Stage: name → license_

**Candidate:** sí

**Marisol:** ¿En qué ciudad y barrio repartirías?

_Stage: license → location_

**Candidate:** Ourense

**Marisol:** Ahora mismo no operamos en Ourense. ¿Tienes otra ciudad cercana de nuestra lista o te apunto a lista de espera?

**Candidate:** Vigo

**Marisol:** Gracias por tu tiempo. No tenemos zona de reparto ahí ahora mismo, así que cierro esta candidatura y te apunto a la lista de espera para cuando abramos.

_Stage: location → disqualified_

_Async check license_check: error (No se recibió ningún número de documento)_

---

**Final stage:** disqualified · **flags:** ['waitlist_out_of_area', 'out_of_area', 'check_error:license_check']

```json
{
  "profile_id": "delivery_courier",
  "qualified": false,
  "outcome": "disqualified",
  "disqualify_reason": "out_of_area",
  "close_reason": "out_of_area",
  "completed_stages": [
    "intro",
    "name",
    "license"
  ],
  "dropped_at_stage": null,
  "duration_seconds": 0,
  "candidate_turns": 5,
  "languages": [
    "es"
  ],
  "data": {
    "full_name": "Ana Ruiz",
    "has_license": true,
    "city": null,
    "zone": null,
    "country": null,
    "in_service_area": null,
    "availability": [],
    "preferred_schedule": null,
    "experience_years": null,
    "experience_platforms": [],
    "start_date": null,
    "license_check": "pending"
  },
  "score": 0.0,
  "recruiter_notes": [],
  "flags": [
    "waitlist_out_of_area",
    "out_of_area"
  ],
  "generated_at": "2026-11-04T14:18:53.503601Z"
}
```
