# Disqualified — city outside service areas after one retry (waitlist)

_Session 24b37041-61c3-4a1e-a3db-00cb6639680c · language es · today 2026-10-11_

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

**Marisol:** Ahora mismo no operamos en esa ciudad. ¿Tienes otra ciudad cercana de nuestra lista o te apunto a lista de espera?

**Candidate:** Vigo

**Marisol:** Gracias por tu tiempo. No tenemos zona de reparto ahí ahora mismo, así que cierro esta candidatura y te apunto a la lista de espera para cuando abramos.

_Stage: location → disqualified_

---

**Final stage:** disqualified · **flags:** ['waitlist_out_of_area', 'out_of_area']

```json
{
  "qualified": false,
  "outcome": "disqualified",
  "disqualify_reason": "out_of_area",
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
    "start_date": null
  },
  "recruiter_notes": [],
  "flags": [
    "waitlist_out_of_area",
    "out_of_area"
  ],
  "generated_at": "2026-10-12T16:43:24.050593Z"
}
```
