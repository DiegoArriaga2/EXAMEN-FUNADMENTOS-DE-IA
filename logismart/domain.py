"""Reglas y clasificación: lógica de dominio sin GUI ni MongoDB."""
import json, re, time
from typing import Literal
from pydantic import BaseModel, ConfigDict
try:
    import ollama
except ImportError:
    ollama = None

PRIORITY = ["baja", "media", "alta", "critica"]
KEYWORDS = {
    "materiales_peligrosos": ["peligroso", "derrame", "fuga", "quimico", "inflamable", "toxico", "corrosivo"],
    "sobrepeso": ["sobrepeso", "excede", "bascula", "exceso de peso", "sobrecarga"],
    "acceso_no_autorizado": ["sin autorizacion", "no autorizado", "acceso denegado", "intruso"],
    "falla_hardware": ["camara", "sensor", "lector", "rfid", "no enciende", "apagado", "danado", "falla electrica"],
    "falla_software": ["sistema", "error", "pantalla", "caido", "no carga", "lento", "software", "aplicacion"],
    "somnolencia_conductor": ["somnolencia", "dormido", "cansancio", "fatiga", "sueno"],
}
BASE_PRIORITY = {"materiales_peligrosos":"critica", "somnolencia_conductor":"alta", "acceso_no_autorizado":"alta", "sobrepeso":"media", "falla_hardware":"media", "falla_software":"baja", "otro":"baja"}
URGENT = ["urgente", "emergencia", "accidente", "incendio", "herido", "critico", "inmediato"]
LABELS = tuple(KEYWORDS) + ("otro",)

def normalize(s): return s.lower().translate(str.maketrans("áéíóúüñ", "aeiouun"))

def extract_entities(text):
    plate=re.search(r"\b[A-Z0-9]{2,3}-\d{2,3}-[A-Z0-9]{1,2}\b",text.upper()); truck=re.search(r"\bCAM-\d+\b",text.upper()); loc=re.search(r"\b(and[eé]n|puerta|muelle|caseta)\s+([\w]+)",text,re.I)
    return {"placa":plate.group(0) if plate else None,"camion_id":truck.group(0) if truck else None,"ubicacion":f"{loc.group(1)} {loc.group(2)}" if loc else None}

def rule_classifier(subject, body):
    text=normalize(subject+" "+body); hits={c:[k for k in words if k in text] for c,words in KEYWORDS.items()}; cat=max(hits,key=lambda c:len(hits[c]))
    if not hits[cat]: cat="otro"
    urgent=[w for w in URGENT if w in text]; p=BASE_PRIORITY[cat]
    if urgent:p=PRIORITY[min(PRIORITY.index(p)+1,3)]
    return {"categoria":cat,"prioridad":p,"entidades":extract_entities(subject+" "+body),"resumen":(subject+": "+body)[:240],"palabras_clave":hits.get(cat,[])+urgent}

def evaluate(P,Q,R,S,certificacion_vigente=True,horario_restringido=False):
    A=P and S and not Q; E=P and (R or Q); A_cert=A and certificacion_vigente; B=R and horario_restringido; A_final=A_cert and not B
    return {"A":A,"E":E,"B":B,"A_final":A_final,"explicacion":[f"A=P∧S∧¬Q={P}∧{S}∧¬{Q}={A}",f"C=certificación vigente={certificacion_vigente}; A con certificación={A_cert}",f"B=R∧H={R}∧{horario_restringido}={B}",f"A final=A∧C∧¬B={A_final}",f"E=P∧(R∨Q)={P}∧({R}∨{Q})={E}"]}

def truth_rows():
    import itertools
    return [dict(P=p,Q=q,R=r,S=s,certificacion_vigente=c,horario_restringido=h,**evaluate(p,q,r,s,c,h)) for p,q,r,s,c,h in itertools.product([False,True],repeat=6)]

class IncidentSchema(BaseModel):
    model_config=ConfigDict(extra="forbid")
    categoria: Literal["materiales_peligrosos","sobrepeso","acceso_no_autorizado","falla_hardware","falla_software","somnolencia_conductor","otro"]
    prioridad: Literal["baja","media","alta","critica"]
    entidades: dict
    resumen: str

def llm_classify(subject,body,model):
    if not ollama:return None,0.0,"Paquete Ollama no instalado"
    prompt=("Responde SOLO JSON exacto: categoria, prioridad, entidades, resumen. categoria una de materiales_peligrosos, sobrepeso, acceso_no_autorizado, falla_hardware, falla_software, somnolencia_conductor, otro. prioridad una de baja, media, alta, critica. entidades es objeto placa, camion_id, ubicacion (texto o null). No uses lista. No inventes.\nAsunto: "+subject+"\nCorreo: "+body)
    start=time.perf_counter(); error=""
    for _ in range(2):
        try:
            raw=ollama.chat(model=model,messages=[{"role":"user","content":prompt}],format="json")["message"]["content"]; payload=json.loads(raw)
            entities=payload.get("entidades") if isinstance(payload.get("entidades"),dict) else {}; extracted=extract_entities(subject+" "+body)
            payload["entidades"]={k:entities.get(k) or extracted[k] for k in extracted}
            data=IncidentSchema.model_validate(payload).model_dump()
            return data,time.perf_counter()-start,""
        except Exception as exc:error=f"{type(exc).__name__}: {exc}"
    return None,time.perf_counter()-start,error

def fuse(rule,llm):
    if not llm:return {**rule,"requiere_revision_humana":False,"motor":"reglas"}
    chosen=max((rule["prioridad"],llm["prioridad"]),key=PRIORITY.index)
    cat=llm["categoria"]
    if cat!=rule["categoria"]:
        safety=[c for c in (rule["categoria"],cat) if c in ("materiales_peligrosos","acceso_no_autorizado","somnolencia_conductor")]
        if safety:cat=safety[0]
    return {**llm,"categoria":cat,"prioridad":chosen,"requiere_revision_humana":rule["categoria"]!=llm["categoria"] or rule["prioridad"]!=llm["prioridad"],"motor":"híbrido"}
