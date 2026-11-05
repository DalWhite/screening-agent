# Construction labourer — qualified, no PRL course (flag for the recruiter, not a rejection)

_Profile construction_laborer (Constructora Horizonte) · session 669a51c0-933e-402c-9c8a-2c8953a2a73c · language es · today 2026-10-11_

**Andrés:** ¡Hola! Soy Andrés, de Constructora Horizonte. Buscamos peones y ayudantes de obra, no hace falta experiencia. Son 10 preguntas cortas, unos 3 minutos. ¿Empezamos?

**Candidate:** Hola, busco trabajo en obra

**Andrés:** ¿Cómo te llamas? Nombre y apellido.

_Stage: intro → name_

**Candidate:** Carlos Martín Ruiz

**Andrés:** ¿Eres mayor de 18 años?

_Stage: name → adult_

**Candidate:** sí, tengo 34 años

**Andrés:** ¿Tienes permiso para trabajar en España (DNI, o NIE con autorización de trabajo)? Sí o no.

_Stage: adult → work_permit_

**Candidate:** sí, tengo NIE con permiso

**Andrés:** Para darte de alta tenemos que verificar tu documento. ¿Me dices tu DNI, NIE o pasaporte? Solo lo usamos para esa verificación.

_Stage: work_permit → id_document_

**Candidate:** mi DNI es 12345678Z

**Andrés:** ¿En qué ciudad o zona vives? Tenemos obras en Madrid, Toledo, Valencia, Sevilla, Málaga y Zaragoza.

_Stage: id_document → location_

**Candidate:** Getafe

**Andrés:** El trabajo es físico: cargar material de hasta 25 kg y estar de pie buena parte de la jornada. ¿Puedes hacerlo sin problema?

_Stage: location → physical_

**Candidate:** ¿Dais vosotros las botas y el casco?

**Andrés:** Sí. La empresa entrega todos los EPIs sin coste: casco, botas de seguridad, guantes, chaleco reflectante, gafas y ropa de trabajo.

El trabajo es físico: cargar material de hasta 25 kg y estar de pie buena parte de la jornada. ¿Puedes hacerlo sin problema?

_RAG: answered from FAQ ppe_es_

**Candidate:** sí, sin problema

**Andrés:** ¿Tienes el curso de PRL de 20 horas (prevención de riesgos en obra)? Si no, no pasa nada: te lo damos nosotros.

_Stage: physical → prl_

**Candidate:** no, no lo tengo

**Andrés:** ¿Has trabajado antes en obra o en algún oficio (albañilería, pintura, encofrado...)? ¿Cuántos años? Si no, di 0.

_Stage: prl → experience_

**Candidate:** 3 años de peón y algo de albañilería

**Andrés:** Las obras empiezan a las 7:00 y cambian de sitio. ¿Cómo irías: vehículo propio o transporte público?

_Stage: experience → transport_

**Candidate:** tengo coche

**Andrés:** Última: ¿desde qué día podrías empezar?

_Stage: transport → start_date_

**Candidate:** el lunes

**Andrés:** ¡Gracias, ya tengo todo! Estoy verificando tus datos y te aviso por aquí en cuanto tenga la respuesta.

_Stage: start_date → verifying_

_Async check id_check: valid (Documento verificado en el registro)_

**Andrés:** ¡Perfecto, cumples los requisitos! Un encargado te llamará en 24-48h para concretar la obra y el día de incorporación. ¡Gracias!

---

**Final stage:** qualified · **flags:** ['needs_prl_training']

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
  "candidate_turns": 12,
  "languages": [
    "es"
  ],
  "data": {
    "full_name": "Carlos Martín Ruiz",
    "is_adult": true,
    "work_permit": true,
    "id_document": "•••••678Z",
    "id_document_type": "dni",
    "city": "Madrid",
    "zone": "Getafe",
    "country": "ES",
    "in_service_area": true,
    "physical_ok": true,
    "has_prl_course": false,
    "construction_experience_years": 3,
    "construction_trades": [
      "peón / ayudante",
      "albañilería"
    ],
    "transport": "own_vehicle",
    "start_date": "2026-10-12",
    "id_check": "valid"
  },
  "score": 75.0,
  "recruiter_notes": [
    "Sin curso PRL de 20h: reservar el curso de la empresa antes del primer día en obra.",
    "Carlos Martín Ruiz — Mayor de edad ✓ — Permiso de trabajo ✓ — DNI •••••678Z — Madrid (Getafe), ES — Trabajo físico ✓ — Curso PRL 20h ✗ — 3 años (peón / ayudante, albañilería) — Vehículo propio — desde"
  ],
  "flags": [
    "needs_prl_training"
  ],
  "generated_at": "2026-11-04T14:18:53.553903Z"
}
```
