"""LogiSmart: práctica modular de reglas, MongoDB, Ollama y GUI.

Instalación: python -m pip install pymongo pydantic ollama
Ejecutar: python logismart_app.py
MongoDB local: mongodb://localhost:27017 (Compass puede mostrar la misma BD).
"""
from __future__ import annotations

import csv
import itertools
import json
import os
import re
import statistics
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk, filedialog
from typing import Optional, Literal
from logismart.domain import evaluate, truth_rows, rule_classifier, extract_entities, llm_classify, fuse
from logismart.repository import MongoStore as Store

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
except ImportError:
    MongoClient = None
    PyMongoError = Exception

try:
    from pydantic import BaseModel, ConfigDict
except ImportError:
    BaseModel = None
    ConfigDict = None

try:
    import ollama
except ImportError:
    ollama = None


MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("LOGISMART_DB", "logismart")
MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
PRIORITY = ["baja", "media", "alta", "critica"]
CATEGORIES = ("materiales_peligrosos", "sobrepeso", "acceso_no_autorizado", "falla_hardware", "falla_software", "somnolencia_conductor", "otro")
KEYWORDS = {
    "materiales_peligrosos": ["peligroso", "derrame", "fuga", "quimico", "inflamable", "toxico", "corrosivo"],
    "sobrepeso": ["sobrepeso", "excede", "bascula", "exceso de peso", "sobrecarga"],
    "acceso_no_autorizado": ["sin autorizacion", "no autorizado", "acceso denegado", "intruso"],
    "falla_hardware": ["camara", "sensor", "lector", "rfid", "no enciende", "apagado", "danado", "falla electrica"],
    "falla_software": ["sistema", "error", "pantalla", "caido", "no carga", "lento", "software", "aplicacion"],
    "somnolencia_conductor": ["somnolencia", "dormido", "cansancio", "fatiga", "sueno"],
}
BASE_PRIORITY = {"materiales_peligrosos": "critica", "somnolencia_conductor": "alta", "acceso_no_autorizado": "alta", "sobrepeso": "media", "falla_hardware": "media", "falla_software": "baja", "otro": "baja"}
URGENT = ["urgente", "emergencia", "accidente", "incendio", "herido", "critico", "inmediato"]
SEED_RISKS = [
    ("Clasificador LLM", "Alucinación o extracción incorrecta", "transparencia", 3, 5, "Validar JSON, mostrar fuentes y exigir revisión humana"),
    ("Clasificador", "Sesgo ante correos con ortografía informal", "sesgo", 4, 4, "Evaluar muestras diversas y corregir vocabulario"),
    ("MongoDB", "Exposición de datos personales del conductor", "privacidad", 3, 5, "Minimizar datos, limitar acceso y definir retención"),
    ("Operación", "Dependencia excesiva de la automatización", "responsabilidad", 3, 5, "Mantener decisión y anulación humana"),
]


def normalize(s: str) -> str:
    return s.lower().translate(str.maketrans("áéíóúüñ", "aeiouun"))


def rule_classifier(subject: str, body: str) -> dict:
    text = normalize(subject + " " + body)
    hits = {c: [k for k in words if k in text] for c, words in KEYWORDS.items()}
    cat = max(hits, key=lambda c: len(hits[c]))
    if not hits[cat]:
        cat = "otro"
    urgent = [w for w in URGENT if w in text]
    p = BASE_PRIORITY[cat]
    if urgent:
        p = PRIORITY[min(PRIORITY.index(p) + 1, 3)]
    return {"categoria": cat, "prioridad": p, "entidades": extract_entities(subject + " " + body), "resumen": (subject + ": " + body)[:240], "palabras_clave": hits.get(cat, []) + urgent}


def extract_entities(text: str) -> dict:
    plate = re.search(r"\b[A-Z0-9]{2,3}-\d{2,3}-[A-Z0-9]{1,2}\b", text.upper())
    truck = re.search(r"\bCAM-\d+\b", text.upper())
    loc = re.search(r"\b(and[eé]n|puerta|muelle|caseta)\s+([\w]+)", text, re.I)
    return {"placa": plate.group(0) if plate else None, "camion_id": truck.group(0) if truck else None, "ubicacion": f"{loc.group(1)} {loc.group(2)}" if loc else None}


def evaluate(P: bool, Q: bool, R: bool, S: bool, certificacion_vigente: bool = True, horario_restringido: bool = False) -> dict:
    A = P and S and (not Q)
    E = P and (R or Q)
    # Extensión 1: certificación vencida bloquea acceso estándar.
    A2 = A and certificacion_vigente
    # Extensión 2: horario restringido para carga peligrosa requiere inspección.
    # Regla 2 no redundante: carga peligrosa en horario restringido se bloquea.
    B = R and horario_restringido
    A_final = A2 and (not B)
    return {"A": A, "E": E, "B": B, "A_final": A_final, "regla_base_A": A, "regla_base_E": E,
            "explicacion": [f"A base=P∧S∧¬Q={P}∧{S}∧¬{Q}={A}", f"A con certificación=A base∧C={A}∧{certificacion_vigente}={A2}", f"B=bloqueo horario=R∧H={R}∧{horario_restringido}={B}", f"A final=A con certificación∧¬B={A2}∧¬{B}={A_final}", f"E=P∧(R∨Q)={P}∧({R}∨{Q})={E}"]}


def truth_rows():
    return [dict(P=p, Q=q, R=r, S=s, certificacion_vigente=c, horario_restringido=h, **evaluate(p,q,r,s,c,h))
            for p,q,r,s,c,h in itertools.product([False, True], repeat=6)]


if BaseModel:
    class IncidentSchema(BaseModel):
        model_config = ConfigDict(extra="forbid")
        categoria: Literal["materiales_peligrosos", "sobrepeso", "acceso_no_autorizado", "falla_hardware", "falla_software", "somnolencia_conductor", "otro"]
        prioridad: Literal["baja", "media", "alta", "critica"]
        entidades: dict
        resumen: str
else:
    IncidentSchema = None


def llm_classify(subject: str, body: str, model: str) -> tuple[Optional[dict], float, str]:
    if not ollama or not IncidentSchema:
        return None, 0.0, "Instala ollama y pydantic para activar el LLM"
    prompt = ("Clasifica el correo y responde SOLO JSON con exactamente estas claves: categoria, prioridad, entidades, resumen. "
              "Categorias: materiales_peligrosos, sobrepeso, acceso_no_autorizado, falla_hardware, falla_software, somnolencia_conductor, otro. "
              "Prioridad: baja, media, alta, critica. entidades debe ser un OBJETO con las claves placa, camion_id y ubicacion; cada valor es texto o null. No uses una lista. No inventes entidades; usa null si faltan.\n" + subject + "\n" + body)
    start = datetime.now()
    error = ""
    for _ in range(2):
        try:
            res = ollama.chat(model=model, messages=[{"role":"user","content":prompt}], format="json")
            raw = res["message"]["content"]
            payload = json.loads(raw)
            # Modelos pequeños a veces devuelven entidades como lista libre.
            # Normalizamos al objeto del contrato y usamos extracción local para
            # no perder placa, camión o ubicación presentes literalmente en correo.
            entities = payload.get("entidades")
            if not isinstance(entities, dict):
                entities = {}
            extracted = extract_entities(subject + " " + body)
            payload["entidades"] = {key: entities.get(key) or extracted[key] for key in extracted}
            data = IncidentSchema(**payload).model_dump()
            return data, (datetime.now()-start).total_seconds(), ""
        except Exception as exc:
            error = str(exc)
    return None, (datetime.now()-start).total_seconds(), error


class Store:
    def __init__(self):
        self.client = self.db = None
        self.error = ""
        if MongoClient:
            try:
                self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1800)
                self.client.admin.command("ping")
                self.db = self.client[DB_NAME]
                self.db.accesos.create_index("fecha")
                self.db.incidentes.create_index([("categoria",1),("fecha",-1)])
            except Exception as exc:
                self.error = str(exc)
                self.db = None

    def put(self, collection, doc):
        if self.db is None:
            raise RuntimeError(self.error or "PyMongo no está instalado")
        return self.db[collection].insert_one(doc).inserted_id

    def list(self, collection, query=None, limit=200):
        if self.db is None: raise RuntimeError(self.error or "MongoDB no está conectado")
        return list(self.db[collection].find(query or {}).sort("fecha", -1).limit(limit))

    def delete(self, collection, ident):
        from bson import ObjectId
        return self.db[collection].delete_one({"_id": ObjectId(ident)}).deleted_count

# La interfaz consume la capa de dominio y el repositorio como servicios.
from logismart.domain import evaluate, truth_rows, rule_classifier, extract_entities, llm_classify, fuse
from logismart.repository import MongoStore as Store


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LogiSmart · centro de control")
        self.geometry("1120x760")
        self.minsize(900, 620)
        self.store = Store()
        self.model = MODEL
        self.review_threshold = "alta"
        self.simulate_email = True
        self._style()
        self._build()
        self.seed_demo()

    def _style(self):
        self.configure(bg="#f3f5f8")
        st = ttk.Style(self); st.theme_use("clam")
        st.configure("TFrame", background="#f3f5f8")
        st.configure("TLabel", background="#f3f5f8", foreground="#172033", font=("Segoe UI",10))
        st.configure("Title.TLabel", font=("Segoe UI",22,"bold"), foreground="#172033")
        st.configure("TButton", font=("Segoe UI",10), padding=8)
        st.configure("Treeview", rowheight=28, font=("Segoe UI",9))
        st.configure("Treeview.Heading", font=("Segoe UI",9,"bold"))

    def _build(self):
        header=ttk.Frame(self,padding=(22,18)); header.pack(fill="x")
        ttk.Label(header,text="LOGISMART",style="Title.TLabel").pack(side="left")
        self.status=ttk.Label(header,text=self.db_status()); self.status.pack(side="right")
        self.nb=ttk.Notebook(self); self.nb.pack(fill="both",expand=True,padx=18,pady=(0,18))
        self.dashboard=ttk.Frame(self.nb,padding=18); self.nb.add(self.dashboard,text="Resumen")
        self.access=ttk.Frame(self.nb,padding=18); self.nb.add(self.access,text="Acceso")
        self.incidents=ttk.Frame(self.nb,padding=18); self.nb.add(self.incidents,text="Incidentes")
        self.trucks=ttk.Frame(self.nb,padding=18); self.nb.add(self.trucks,text="Camiones")
        self.assistant=ttk.Frame(self.nb,padding=18); self.nb.add(self.assistant,text="Asistente RAG")
        self.risks=ttk.Frame(self.nb,padding=18); self.nb.add(self.risks,text="Riesgos éticos")
        self.truth=ttk.Frame(self.nb,padding=18); self.nb.add(self.truth,text="Tabla de verdad")
        self.config=ttk.Frame(self.nb,padding=18); self.nb.add(self.config,text="Configuración")
        self.admin=ttk.Frame(self.nb,padding=18);self.nb.add(self.admin,text="CRUD MongoDB")
        self.build_dashboard(); self.build_access(); self.build_incidents(); self.build_trucks(); self.build_assistant(); self.build_risks(); self.build_truth(); self.build_config();self.build_admin()

    def db_status(self):
        return "● MongoDB conectado" if self.store.db is not None else "○ Sin MongoDB: instala/inicia MongoDB y reinicia la app"

    def db_error(self, exc):
        messagebox.showerror("MongoDB", f"No se pudo completar la operación.\n{exc}\n\nRevisa que MongoDB esté activo y MONGODB_URI sea correcta.")

    def seed_demo(self):
        if self.store.db is None: return
        try:
            if self.store.db.camiones.count_documents({}) == 0:
                self.store.db.camiones.insert_many([
                    {"placa":"ABC-123-D","camion_id":"CAM-102","empresa":"Transportes Demo","autorizacion":True,"certificacion_conductor":True},
                    {"placa":"XYZ-456-A","camion_id":"CAM-205","empresa":"Logística Norte","autorizacion":True,"certificacion_conductor":False}])
            if self.store.db.riesgos_eticos.count_documents({}) == 0:
                self.store.db.riesgos_eticos.insert_many([self.risk_doc(*r) for r in SEED_RISKS])
        except Exception: pass

    def card(self, parent, title, value):
        box=ttk.LabelFrame(parent,text=title,padding=18); box.pack(side="left",fill="x",expand=True,padx=6,pady=12)
        label=ttk.Label(box,text=str(value),font=("Segoe UI",24,"bold")); label.pack(); return label

    def build_dashboard(self):
        ttk.Label(self.dashboard,text="Resumen operativo",style="Title.TLabel").pack(anchor="w")
        filters=ttk.Frame(self.dashboard); filters.pack(fill="x",pady=(8,0))
        ttk.Label(filters,text="Desde (AAAA-MM-DD)").pack(side="left")
        self.date_from=tk.StringVar(); ttk.Entry(filters,textvariable=self.date_from,width=13).pack(side="left",padx=5)
        ttk.Label(filters,text="Hasta").pack(side="left")
        self.date_to=tk.StringVar(); ttk.Entry(filters,textvariable=self.date_to,width=13).pack(side="left",padx=5)
        ttk.Button(filters,text="Aplicar filtro",command=self.refresh_dashboard).pack(side="left",padx=8)
        row=ttk.Frame(self.dashboard); row.pack(fill="x")
        self.k_cam=self.card(row,"Camiones atendidos","—"); self.k_inc=self.card(row,"Incidentes abiertos","—"); self.k_risk=self.card(row,"Riesgos críticos","—")
        ttk.Button(self.dashboard,text="Actualizar indicadores",command=self.refresh_dashboard).pack(anchor="e")
        ttk.Button(self.dashboard,text="Incidentes por categoría y semana",command=self.show_weekly_aggregation).pack(anchor="e",pady=3)
        report=ttk.Frame(self.dashboard);report.pack(anchor="e",pady=4)
        self.report_collection=tk.StringVar(value="incidentes")
        ttk.Label(report,text="Colección para exportar").pack(side="left")
        ttk.Combobox(report,textvariable=self.report_collection,values=["camiones","accesos","incidentes","riesgos_eticos","evaluaciones_llm"],state="readonly",width=20).pack(side="left",padx=5)
        ttk.Button(report,text="Exportar JSON / CSV / PDF",command=self.export_report).pack(side="left")
        self.refresh_dashboard()

    def refresh_dashboard(self):
        try:
            query={}
            if self.date_from.get().strip() or self.date_to.get().strip():
                bounds={}
                if self.date_from.get().strip(): bounds["$gte"]=datetime.strptime(self.date_from.get().strip(),"%Y-%m-%d").replace(tzinfo=timezone.utc)
                if self.date_to.get().strip(): bounds["$lt"]=(datetime.strptime(self.date_to.get().strip(),"%Y-%m-%d")+timedelta(days=1)).replace(tzinfo=timezone.utc)
                query["fecha"]=bounds
            unique_plates=self.store.db.accesos.distinct("placa",query)
            attended=len({plate for plate in unique_plates if plate})+self.store.db.accesos.count_documents({**query,"$or":[{"placa":None},{"placa":""}]})
            self.k_cam.config(text=str(attended))
            iq=dict(query); iq["estado"]={"$ne":"cerrado"}
            self.k_inc.config(text=str(self.store.db.incidentes.count_documents(iq)))
            risk_query=dict(query);risk_query["puntaje_residual"]={"$gte":17}
            self.k_risk.config(text=str(self.store.db.riesgos_eticos.count_documents(risk_query)))
            self.status.config(text=self.db_status())
        except Exception as exc:
            if hasattr(self,"status"): self.status.config(text=f"MongoDB / filtro: {exc}")

    def show_weekly_aggregation(self):
        try:
            data=self.store.incident_weekly_aggregation()
            lines=[f"{d['_id']['year']}-S{d['_id']['week']:02d} · {d['_id']['categoria']}: {d['cantidad']}" for d in data]
            messagebox.showinfo("Agregación semanal", "\n".join(lines) if lines else "Aún no hay incidentes para agrupar.")
        except Exception as exc: self.db_error(exc)

    def export_report(self):
        collection=self.report_collection.get()
        ext=filedialog.asksaveasfilename(defaultextension=".json",filetypes=[("JSON","*.json"),("CSV","*.csv"),("PDF","*.pdf")],initialfile=f"reporte_{collection}")
        if not ext:return
        try:
            records=self.store.list(collection,limit=5000)
            rows=[{k:(str(v) if k=="_id" else v.isoformat() if hasattr(v,"isoformat") else v) for k,v in d.items()} for d in records]
            suffix=os.path.splitext(ext)[1].lower()
            if suffix==".json":
                with open(ext,"w",encoding="utf-8") as f:json.dump(rows,f,ensure_ascii=False,indent=2,default=str)
            elif suffix==".csv":
                keys=sorted({key for row in rows for key in row})
                with open(ext,"w",newline="",encoding="utf-8-sig") as f:
                    w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows({k:json.dumps(row.get(k),ensure_ascii=False,default=str) if isinstance(row.get(k),(dict,list)) else row.get(k) for k in keys} for row in rows)
            elif suffix==".pdf":
                from reportlab.lib.pagesizes import letter
                from reportlab.lib.styles import getSampleStyleSheet
                from reportlab.lib.units import inch
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
                from xml.sax.saxutils import escape
                styles=getSampleStyleSheet();story=[Paragraph(f"Reporte LogiSmart · {collection}",styles["Title"]),Spacer(1,0.2*inch),Paragraph(f"Generado: {datetime.now().astimezone().isoformat()} · Registros: {len(rows)}",styles["Normal"]),Spacer(1,0.15*inch)]
                for number,row in enumerate(rows,1):
                    story.append(Paragraph(f"Registro {number}",styles["Heading3"]))
                    for key,value in row.items():
                        safe=escape(str(value)).replace("\n","<br/>")
                        story.append(Paragraph(f"<b>{escape(str(key))}:</b> {safe}",styles["BodyText"]))
                    story.append(Spacer(1,0.12*inch))
                SimpleDocTemplate(ext,pagesize=letter,rightMargin=0.55*inch,leftMargin=0.55*inch).build(story)
            messagebox.showinfo("Exportación",f"{len(rows)} registros exportados a:\n{ext}")
        except Exception as exc:messagebox.showerror("Exportación",str(exc))

    def build_access(self):
        ttk.Label(self.access,text="Evaluar acceso de camión",style="Title.TLabel").pack(anchor="w",pady=(0,12))
        frm=ttk.Frame(self.access); frm.pack(anchor="w")
        self.flags={}
        for i,(key,label) in enumerate([("P","Autorización previa"),("Q","Exceso de peso"),("R","Material peligroso"),("S","Conductor certificado"),("cert","Certificación vigente"),("horario","Horario restringido")]):
            var=tk.BooleanVar(value=(key in ("P","S","cert"))); self.flags[key]=var
            ttk.Checkbutton(frm,text=label,variable=var,command=self.live_eval).grid(row=i//2,column=i%2,sticky="w",padx=12,pady=8)
        ttk.Label(self.access,text="Placa (opcional)").pack(anchor="w",pady=(14,3)); self.plate=tk.StringVar(); ttk.Entry(self.access,textvariable=self.plate,width=30).pack(anchor="w")
        ttk.Button(self.access,text="Guardar evaluación",command=self.save_access).pack(anchor="w",pady=12)
        self.access_result=ttk.Label(self.access,text="Activa las premisas para ver el resultado.",font=("Segoe UI",12,"bold")); self.access_result.pack(anchor="w",pady=8)
        self.access_expl=tk.Text(self.access,height=7,width=100,wrap="word"); self.access_expl.pack(fill="x",pady=8)
        self.live_eval()

    def live_eval(self):
        f=self.flags; out=evaluate(f["P"].get(),f["Q"].get(),f["R"].get(),f["S"].get(),f["cert"].get(),f["horario"].get())
        self.access_result.config(text=f"{'🟢 ACCESO ESTÁNDAR' if out['A_final'] else '🔴 ACCESO ESTÁNDAR DENEGADO'}    {'🟠 INSPECCIÓN ESPECIAL' if out['E'] else '🟢 SIN INSPECCIÓN ESPECIAL'}    {'⛔ BLOQUEO HORARIO' if out['B'] else ''}")
        self.access_expl.delete("1.0","end"); self.access_expl.insert("end","\n".join(out["explicacion"]))

    def save_access(self):
        f=self.flags; out=evaluate(f["P"].get(),f["Q"].get(),f["R"].get(),f["S"].get(),f["cert"].get(),f["horario"].get())
        try:
            self.store.put("accesos",{"P":f["P"].get(),"Q":f["Q"].get(),"R":f["R"].get(),"S":f["S"].get(),"certificacion_vigente":f["cert"].get(),"horario_restringido":f["horario"].get(),"placa":self.plate.get().strip().upper() or None,"resultado":{"A":out["A"],"E":out["E"],"B":out["B"],"A_final":out["A_final"]},"explicacion":out["explicacion"],"fecha":datetime.now(timezone.utc),"operador":"operador_demo"})
            messagebox.showinfo("Guardado","Evaluación registrada en accesos.")
        except Exception as exc: self.db_error(exc)

    def build_incidents(self):
        ttk.Label(self.incidents,text="Bandeja de incidentes",style="Title.TLabel").pack(anchor="w")
        form=ttk.Frame(self.incidents); form.pack(fill="x",pady=10)
        self.sender=tk.StringVar(value="operador@logismart.local"); self.subject=tk.StringVar()
        ttk.Label(form,text="Remitente").grid(row=0,column=0,sticky="w"); ttk.Entry(form,textvariable=self.sender,width=34).grid(row=1,column=0,padx=(0,10))
        ttk.Label(form,text="Asunto").grid(row=0,column=1,sticky="w"); ttk.Entry(form,textvariable=self.subject,width=60).grid(row=1,column=1,sticky="ew")
        ttk.Label(self.incidents,text="Correo / descripción").pack(anchor="w"); self.body=tk.Text(self.incidents,height=6,wrap="word"); self.body.pack(fill="x",pady=4)
        bar=ttk.Frame(self.incidents); bar.pack(fill="x",pady=6)
        ttk.Button(bar,text="Clasificar + guardar",command=self.classify_save).pack(side="left")
        self.inc_status=tk.StringVar(value="nuevo"); ttk.Combobox(bar,textvariable=self.inc_status,values=["nuevo","en_atencion","cerrado"],state="readonly",width=15).pack(side="left",padx=8)
        ttk.Button(bar,text="Actualizar estado",command=self.update_incident).pack(side="left")
        self.inc_category=tk.StringVar();self.inc_priority=tk.StringVar()
        ttk.Label(bar,text="Categoría").pack(side="left",padx=(15,2));ttk.Combobox(bar,textvariable=self.inc_category,values=list(CATEGORIES),width=22).pack(side="left")
        ttk.Label(bar,text="Prioridad").pack(side="left",padx=(6,2));ttk.Combobox(bar,textvariable=self.inc_priority,values=PRIORITY,width=10).pack(side="left")
        ttk.Button(bar,text="Guardar corrección",command=self.edit_incident).pack(side="left",padx=5)
        self.eval_status=tk.StringVar(value="Sin evaluación experimental");ttk.Label(self.incidents,textvariable=self.eval_status).pack(anchor="w")
        ttk.Button(self.incidents,text="Evaluar conjunto etiquetado (mínimo 30 correos)",command=self.start_dataset_evaluation).pack(anchor="w",pady=4)
        self.llm_status=tk.StringVar(value="Listo para clasificar")
        ttk.Label(self.incidents,textvariable=self.llm_status).pack(anchor="w")
        self.inc_tree=ttk.Treeview(self.incidents,columns=("categoria","prioridad","estado","fecha"),show="headings",height=9)
        for col,w in [("categoria",220),("prioridad",100),("estado",130),("fecha",220)]: self.inc_tree.heading(col,text=col.title()); self.inc_tree.column(col,width=w)
        self.inc_tree.pack(fill="both",expand=True,pady=8);self.inc_tree.bind("<<TreeviewSelect>>",self.select_incident)
        self.inc_detail=tk.Text(self.incidents,height=5,wrap="word",state="disabled");self.inc_detail.pack(fill="x",pady=5)
        self.refresh_incidents()

    def start_dataset_evaluation(self):
        source=filedialog.askopenfilename(title="Selecciona el CSV con categoria_real",filetypes=[("CSV","*.csv")])
        if not source:return
        target=filedialog.asksaveasfilename(title="Guardar resultados del experimento",defaultextension=".json",initialfile="resultados_experimento.json",filetypes=[("JSON","*.json")])
        if not target:return
        try:
            with open(source,encoding="utf-8-sig",newline="") as f:rows=list(csv.DictReader(f))
            if len(rows)<30 or not rows or "categoria_real" not in rows[0]:
                messagebox.showerror("Conjunto insuficiente","El CSV debe tener al menos 30 filas y una columna categoria_real.");return
            if any((row.get("categoria_real") or "").strip() not in CATEGORIES for row in rows):
                messagebox.showerror("Etiquetas inválidas","Revisa categoria_real: usa las categorías mostradas en la aplicación.");return
        except Exception as exc:messagebox.showerror("CSV",str(exc));return
        self.eval_status.set(f"Evaluando {len(rows)} correos con reglas, Ollama e híbrido… puede tardar varios minutos.")
        threading.Thread(target=self._dataset_worker,args=(rows,target),daemon=True).start()

    def _dataset_worker(self,rows,target):
        from logismart.domain import fuse
        details=[];latencies={"reglas":[],"llm":[],"hibrido":[]};evaluation_docs=[]
        for index,row in enumerate(rows,1):
            subject=row.get("asunto","");body=row.get("correo",row.get("cuerpo",""));real=row["categoria_real"].strip()
            start=time.perf_counter();rule=rule_classifier(subject,body);rules_latency=time.perf_counter()-start
            llm,llm_latency,error=llm_classify(subject,body,self.model)
            start=time.perf_counter();hybrid=fuse(rule,llm);fusion_latency=time.perf_counter()-start
            hybrid_latency=rules_latency+llm_latency+fusion_latency
            latencies["reglas"].append(rules_latency);latencies["llm"].append(llm_latency);latencies["hibrido"].append(hybrid_latency)
            details.append({"id":row.get("id",index),"categoria_real":real,"reglas":rule["categoria"],"llm":llm["categoria"] if llm else None,"hibrido":hybrid["categoria"],"prioridad_llm":llm["prioridad"] if llm else None,"latencia_segundos":{"reglas":round(rules_latency,6),"llm":round(llm_latency,4),"hibrido":round(hybrid_latency,4)},"error":error})
            evaluation_docs.append({"prompt":f"{subject}\n{body}","respuesta":llm,"modelo":self.model if llm else "fallo_llm","latencia_segundos":llm_latency,"latencia_reglas":rules_latency,"latencia_hibrido":hybrid_latency,"coincidio_con_reglas":bool(llm and rule["categoria"]==llm["categoria"] and rule["prioridad"]==llm["prioridad"]),"categoria_real":real,"categoria_reglas":rule["categoria"],"categoria_hibrido":hybrid["categoria"],"error":error,"fecha":datetime.now(timezone.utc)})
        def metrics(key):
            evaluated=[r for r in details if r[key] is not None]
            matrix={actual:{pred:0 for pred in CATEGORIES} for actual in CATEGORIES}
            for r in evaluated:matrix[r["categoria_real"]][r[key]]+=1
            correct=sum(r["categoria_real"]==r[key] for r in evaluated)
            return {"n_evaluados":len(evaluated),"n_fallidos":len(details)-len(evaluated),"exactitud":correct/len(evaluated) if evaluated else None,"matriz_confusion":matrix}
        result={"fecha":datetime.now(timezone.utc).isoformat(),"modelo":self.model,"n_correos":len(rows),"metricas":{"reglas":metrics("reglas"),"llm":metrics("llm"),"hibrido":metrics("hibrido")},"latencia_segundos":{name:{"promedio":statistics.mean(values) if values else None,"mediana":statistics.median(values) if values else None,"muestras":values} for name,values in latencies.items()},"detalle":details}
        try:
            with open(target,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
            for doc in evaluation_docs:self.store.put("evaluaciones_llm",doc)
            self.after(0,lambda:self._dataset_done(target,result,None))
        except Exception as exc:
            error_message=str(exc)
            self.after(0,lambda:self._dataset_done(target,result,error_message))

    def _dataset_done(self,target,result,error):
        if error:
            self.eval_status.set("El experimento terminó, pero hubo un error al registrar la evaluación en MongoDB.")
            messagebox.showerror("Experimento",f"{error}\nJSON de resultados: {target}");return
        self.eval_status.set(f"Experimento completo: {result['n_correos']} correos · resultados guardados en MongoDB y {target}")
        m=result["metricas"]
        fmt=lambda v:"No disponible" if v is None else f"{v:.1%}"
        latency=result["latencia_segundos"]
        messagebox.showinfo("Métricas del experimento",f"Reglas: exactitud {fmt(m['reglas']['exactitud'])} · latencia media {latency['reglas']['promedio']*1000:.3f} ms\nLLM: exactitud {fmt(m['llm']['exactitud'])} ({m['llm']['n_evaluados']} válidos, {m['llm']['n_fallidos']} fallidos) · latencia media {latency['llm']['promedio']:.2f} s\nHíbrido: exactitud {fmt(m['hibrido']['exactitud'])} · latencia media {latency['hibrido']['promedio']:.2f} s\n\nMatrices completas y detalle: {target}")

    def classify_save(self):
        body=self.body.get("1.0","end").strip(); subject=self.subject.get().strip()
        if not body: messagebox.showwarning("Falta información","Pega el contenido del correo."); return
        self.llm_status.set("Clasificando con Ollama; se usará respaldo por reglas si hace falta…");self.update_idletasks()
        rules=rule_classifier(subject,body); llm,latency,error=llm_classify(subject,body,self.model)
        final=fuse(rules,llm); source=final["motor"]
        if PRIORITY.index(final["prioridad"])>=PRIORITY.index(self.review_threshold):final["requiere_revision_humana"]=True
        doc={"correo_original":{"remitente":self.sender.get(),"asunto":subject,"cuerpo":body},"categoria":final["categoria"],"prioridad":final["prioridad"],"datos_extraidos":final.get("entidades",rules["entidades"]),"resumen":final.get("resumen",rules["resumen"]),"estado":"nuevo","fecha":datetime.now(timezone.utc),"clasificador":source,"requiere_revision_humana":final["requiere_revision_humana"],"modo_correo":"simulacion" if self.simulate_email else "manual_sin_envio","historial":[{"evento":"Clasificación inicial","categoria":final["categoria"],"prioridad":final["prioridad"],"fecha":datetime.now(timezone.utc)}]}
        try:
            ident=self.store.put("incidentes",doc)
            self.store.put("evaluaciones_llm",{"prompt":subject+"\n"+body,"respuesta":llm,"modelo":self.model if llm else "reglas","latencia_segundos":latency,"coincidio_con_reglas":bool(llm and rules["categoria"]==llm["categoria"] and rules["prioridad"]==llm["prioridad"]),"error":error,"fecha":datetime.utcnow()})
            fallback_note = "Ollama no devolvió el formato esperado; se usó el clasificador por reglas." if error and not llm else ""
            messagebox.showinfo("Clasificación",f"Categoría: {final['categoria']}\nPrioridad: {final['prioridad']}\nMotor: {source}\nRevisión humana: {'sí' if doc['requiere_revision_humana'] else 'no'}\n\n{fallback_note}")
            self.refresh_incidents(); self.refresh_dashboard()
            self.llm_status.set(f"Clasificación guardada · {source} · {latency:.2f} s")
        except Exception as exc:
            self.llm_status.set("No se pudo guardar la clasificación.");self.db_error(exc)

    def refresh_incidents(self):
        if not hasattr(self,"inc_tree"): return
        self.inc_tree.delete(*self.inc_tree.get_children())
        try:
            for d in self.store.list("incidentes"):
                self.inc_tree.insert("","end",iid=str(d["_id"]),values=(d.get("categoria"),d.get("prioridad"),d.get("estado"),str(d.get("fecha",""))[:19]))
        except Exception: pass

    def update_incident(self):
        sel=self.inc_tree.selection()
        if not sel: messagebox.showwarning("Selecciona un incidente","Elige una fila primero."); return
        try:
            self.store.update("incidentes",sel[0],{"estado":self.inc_status.get(),"actualizado":datetime.now(timezone.utc)},[{"fecha":datetime.now(timezone.utc).isoformat(),"evento":"Cambio de estado","estado":self.inc_status.get()}]); self.refresh_incidents(); self.refresh_dashboard();self.select_incident()
        except Exception as exc: self.db_error(exc)

    def select_incident(self,event=None):
        selected=self.inc_tree.selection()
        if not selected:return
        try:
            d=next((item for item in self.store.list("incidentes") if str(item["_id"])==selected[0]),None)
            if not d:return
            self.inc_category.set(d.get("categoria","otro"));self.inc_priority.set(d.get("prioridad","baja"));self.inc_status.set(d.get("estado","nuevo"))
            detail={"correo_original":d.get("correo_original"),"resumen":d.get("resumen"),"datos_extraidos":d.get("datos_extraidos"),"clasificador":d.get("clasificador"),"requiere_revision_humana":d.get("requiere_revision_humana"),"historial":d.get("historial",[])}
            self.inc_detail.configure(state="normal");self.inc_detail.delete("1.0","end");self.inc_detail.insert("end",json.dumps(detail,ensure_ascii=False,indent=2,default=str));self.inc_detail.configure(state="disabled")
        except Exception as exc:self.db_error(exc)

    def edit_incident(self):
        selected=self.inc_tree.selection()
        category=self.inc_category.get();priority=self.inc_priority.get()
        if not selected:messagebox.showwarning("Selecciona incidente","Selecciona una fila.");return
        if category not in CATEGORIES or priority not in PRIORITY:messagebox.showwarning("Valores no válidos","Elige una categoría y prioridad de la lista.");return
        try:
            self.store.update("incidentes",selected[0],{"categoria":category,"prioridad":priority,"editado_manualmente":True,"actualizado":datetime.now(timezone.utc)},[{"fecha":datetime.now(timezone.utc).isoformat(),"evento":"Clasificación corregida manualmente","categoria":category,"prioridad":priority}]);self.refresh_incidents();self.select_incident()
        except Exception as exc:self.db_error(exc)

    def build_trucks(self):
        ttk.Label(self.trucks,text="Catálogo de camiones · alta, consulta, edición y baja",style="Title.TLabel").pack(anchor="w")
        form=ttk.Frame(self.trucks); form.pack(fill="x",pady=10)
        self.truck_vars=[tk.StringVar() for _ in range(5)]
        labels=["Placa","ID camión","Empresa","Autorizado","Certificación conductor"]
        for i,(var,label) in enumerate(zip(self.truck_vars,labels)):
            ttk.Label(form,text=label).grid(row=0,column=i,sticky="w")
            if i>2: ttk.Combobox(form,textvariable=var,values=["Sí","No"],state="readonly",width=15).grid(row=1,column=i,padx=3)
            else: ttk.Entry(form,textvariable=var,width=22).grid(row=1,column=i,padx=3)
        bar=ttk.Frame(self.trucks);bar.pack(fill="x")
        ttk.Button(bar,text="Guardar / aplicar edición",command=self.save_truck).pack(side="left")
        ttk.Button(bar,text="Limpiar",command=self.clear_truck).pack(side="left",padx=5)
        ttk.Button(bar,text="Eliminar seleccionado",command=self.delete_truck).pack(side="right")
        self.truck_tree=ttk.Treeview(self.trucks,columns=("placa","camion_id","empresa","autorizacion","certificacion_conductor"),show="headings",height=14)
        for c,w in [("placa",150),("camion_id",130),("empresa",240),("autorizacion",120),("certificacion_conductor",200)]:self.truck_tree.heading(c,text=c);self.truck_tree.column(c,width=w)
        self.truck_tree.pack(fill="both",expand=True,pady=8);self.truck_tree.bind("<<TreeviewSelect>>",self.select_truck);self.refresh_trucks()

    def refresh_trucks(self):
        if not hasattr(self,"truck_tree"):return
        self.truck_tree.delete(*self.truck_tree.get_children())
        try:
            for d in self.store.list("camiones"):
                self.truck_tree.insert("","end",iid=str(d["_id"]),values=(d.get("placa"),d.get("camion_id"),d.get("empresa"),"Sí" if d.get("autorizacion") else "No","Sí" if d.get("certificacion_conductor") else "No"))
        except Exception as exc:self.status.config(text=f"Camiones: {exc}")

    def select_truck(self,event=None):
        sel=self.truck_tree.selection()
        if not sel:return
        vals=self.truck_tree.item(sel[0],"values")
        for var,val in zip(self.truck_vars,vals):var.set(val)

    def clear_truck(self):
        self.truck_tree.selection_remove(self.truck_tree.selection())
        for var in self.truck_vars:var.set("")

    def save_truck(self):
        placa,camion_id,empresa,auth,cert=[v.get().strip() for v in self.truck_vars]
        if not placa or not camion_id or not empresa or auth not in ("Sí","No") or cert not in ("Sí","No"):
            messagebox.showwarning("Datos incompletos","Completa los cinco campos; elige Sí o No para autorización y certificación.");return
        doc={"placa":placa.upper(),"camion_id":camion_id.upper(),"empresa":empresa,"autorizacion":auth=="Sí","certificacion_conductor":cert=="Sí"}
        try:
            selected=self.truck_tree.selection()
            if selected:self.store.update("camiones",selected[0],doc,[{"fecha":datetime.now(timezone.utc).isoformat(),"evento":"Ficha actualizada"}])
            else:self.store.put("camiones",doc)
            self.refresh_trucks();self.clear_truck();self.refresh_dashboard()
        except Exception as exc:self.db_error(exc)

    def delete_truck(self):
        selected=self.truck_tree.selection()
        if not selected:messagebox.showwarning("Selecciona un camión","Selecciona una fila para eliminar.");return
        if not messagebox.askyesno("Confirmar baja","¿Eliminar este camión del catálogo?"):return
        try:self.store.delete("camiones",selected[0]);self.refresh_trucks();self.clear_truck();self.refresh_dashboard()
        except Exception as exc:self.db_error(exc)

    def build_assistant(self):
        ttk.Label(self.assistant,text="Asistente explicativo · responde usando registros recuperados",style="Title.TLabel").pack(anchor="w")
        self.chat=tk.Text(self.assistant,state="disabled",wrap="word"); self.chat.pack(fill="both",expand=True,pady=10)
        row=ttk.Frame(self.assistant); row.pack(fill="x"); self.question=tk.StringVar(); ttk.Entry(row,textvariable=self.question).pack(side="left",fill="x",expand=True); ttk.Button(row,text="Preguntar",command=self.ask).pack(side="left",padx=6)

    def ask(self):
        q=self.question.get().strip()
        if not q:return
        self.question.set(""); self.write_chat("Tú",q)
        try:
            terms=re.findall(r"CAM-\d+|[A-Z0-9]{2,3}-\d{2,3}-[A-Z0-9]{1,2}",q.upper())
            docs=[]
            for term in terms:
                docs += [{"_coleccion":"accesos", **d} for d in self.store.list("accesos",{"$or":[{"placa":term},{"camion_id":term}]},10)]
                docs += [{"_coleccion":"camiones", **d} for d in self.store.list("camiones",{"$or":[{"placa":term},{"camion_id":term}]},10)]
            if not docs:
                self.write_chat("Asistente","No tengo información en los registros consultados para responder."); return
            context=json.dumps([{k:(str(v) if k=="_id" else v) for k,v in d.items()} for d in docs],default=str,ensure_ascii=False)
            facts=[]
            for d in docs:
                if d.get("_coleccion")=="accesos":
                    result=d.get("resultado",{})
                    facts.append(f"Acceso {d.get('_id')}: placa {d.get('placa') or 'no indicada'}; acceso estándar {'AUTORIZADO' if result.get('A_final', result.get('A')) else 'NO autorizado'}; inspección especial {'REQUERIDA' if result.get('E') else 'NO requerida'}; bloqueo horario={'sí' if result.get('B') else 'no'}; premisas P={d.get('P')}, Q={d.get('Q')}, R={d.get('R')}, S={d.get('S')}; explicación: {'; '.join(d.get('explicacion', []))}.")
                elif d.get("_coleccion")=="camiones":
                    facts.append(f"Camión {d.get('_id')}: placa {d.get('placa')}; ID {d.get('camion_id')}; empresa {d.get('empresa')}; autorización previa {d.get('autorizacion')}; certificación de conductor {d.get('certificacion_conductor')}.")
            evidence="\n".join(facts)
            if ollama:
                res=ollama.chat(model=self.model,messages=[{"role":"system","content":"Eres un asistente de operaciones. Contesta en español solo con los hechos explícitos de los registros MongoDB recuperados. Interpreta resultado.A=true como acceso estándar autorizado y resultado.E=true como inspección requerida. Si hay un registro de acceso, responde la decisión claramente; no digas que falta información. Si no hay hechos pertinentes, responde exactamente 'No tengo información'. No inventes datos."},{"role":"user","content":f"Pregunta: {q}\nHechos extraídos de MongoDB:\n{evidence}\nRegistros completos (referencia): {context}"}])
                answer=res["message"]["content"]
                if "no tengo información" in answer.lower() and evidence:
                    answer=evidence
            else: answer=evidence or ("Registros recuperados: "+context)
            self.write_chat("Asistente",answer+"\nFuentes: "+", ".join(str(d.get("_id")) for d in docs))
        except Exception as exc: self.write_chat("Sistema",f"No se pudo consultar MongoDB: {exc}")

    def write_chat(self,who,msg):
        self.chat.configure(state="normal"); self.chat.insert("end",f"{who}: {msg}\n\n"); self.chat.configure(state="disabled"); self.chat.see("end")

    def risk_doc(self,module,desc,category,p,i,mitigation):
        return {"modulo":module,"descripcion":desc,"categoria":category,"probabilidad":p,"impacto":i,"puntaje_inicial":p*i,"mitigacion":mitigation,"probabilidad_residual":max(1,p-2),"impacto_residual":max(1,i-2),"puntaje_residual":max(1,p-2)*max(1,i-2),"historico":[{"fecha":datetime.utcnow().isoformat(),"evento":"Alta inicial"}],"fecha":datetime.utcnow()}

    def build_risks(self):
        ttk.Label(self.risks,text="Matriz de riesgos éticos",style="Title.TLabel").pack(anchor="w")
        form=ttk.Frame(self.risks); form.pack(fill="x",pady=8)
        self.risk_vars=[tk.StringVar() for _ in range(6)]; labels=["Módulo","Descripción","Categoría","Prob. 1–5","Impacto 1–5","Mitigación"]
        for i,(v,l) in enumerate(zip(self.risk_vars,labels)):
            ttk.Label(form,text=l).grid(row=0,column=i,sticky="w")
            if i==2:ttk.Combobox(form,textvariable=v,values=["sesgo","privacidad","transparencia","seguridad","responsabilidad","otro"],width=18).grid(row=1,column=i,padx=2)
            else:ttk.Entry(form,textvariable=v,width=20).grid(row=1,column=i,padx=2)
        ttk.Button(form,text="Agregar riesgo",command=self.add_risk).grid(row=1,column=6,padx=6)
        self.risk_tree=ttk.Treeview(self.risks,columns=("modulo","categoria","inicial","residual","mitigacion"),show="headings",height=12)
        for c,w in [("modulo",150),("categoria",125),("inicial",90),("residual",90),("mitigacion",480)]:self.risk_tree.heading(c,text=c.title());self.risk_tree.column(c,width=w)
        self.risk_tree.pack(fill="both",expand=True);self.risk_tree.bind("<<TreeviewSelect>>",self.select_risk)
        actions=ttk.Frame(self.risks);actions.pack(fill="x")
        ttk.Button(actions,text="Guardar edición",command=self.edit_risk).pack(side="left")
        ttk.Button(actions,text="Limpiar formulario",command=self.clear_risk).pack(side="left",padx=5)
        ttk.Button(actions,text="Eliminar seleccionado",command=self.delete_risk).pack(side="right",pady=6)
        self.risk_chart=tk.Canvas(self.risks,height=150,bg="white",highlightthickness=1,highlightbackground="#d8dee9");self.risk_chart.pack(fill="x",pady=6)
        self.refresh_risks()

    def add_risk(self):
        try:
            module,desc,cat,p,i,mit=[v.get().strip() for v in self.risk_vars]
            if not module or not desc or not mit: raise ValueError("Completa módulo, descripción y mitigación.")
            if cat not in ("sesgo","privacidad","transparencia","seguridad","responsabilidad","otro"):raise ValueError("Elige una categoría ética válida.")
            p=int(p); i=int(i)
            if not 1<=p<=5 or not 1<=i<=5: raise ValueError("Probabilidad e impacto deben estar entre 1 y 5.")
            self.store.put("riesgos_eticos",self.risk_doc(module,desc,cat,p,i,mit)); self.refresh_risks(); self.refresh_dashboard()
        except Exception as exc: messagebox.showerror("Validación",str(exc))

    def refresh_risks(self):
        if not hasattr(self,"risk_tree"):return
        self.risk_tree.delete(*self.risk_tree.get_children())
        try:
            for d in self.store.list("riesgos_eticos"):
                self.risk_tree.insert("","end",iid=str(d["_id"]),values=(d.get("modulo"),d.get("categoria"),d.get("puntaje_inicial"),d.get("puntaje_residual"),d.get("mitigacion")))
            self.draw_risk_chart()
        except Exception:pass

    def draw_risk_chart(self):
        c=self.risk_chart;c.delete("all")
        try: risks=self.store.list("riesgos_eticos")
        except Exception:return
        groups={"Bajo":0,"Medio":0,"Alto":0,"Crítico":0}
        for r in risks:
            score=int(r.get("puntaje_residual",0));groups["Crítico" if score>=17 else "Alto" if score>=10 else "Medio" if score>=5 else "Bajo"]+=1
        colors={"Bajo":"#4caf50","Medio":"#f0b429","Alto":"#ef8354","Crítico":"#d64550"};x=30
        for label,count in groups.items():
            width=max(24,count*45);c.create_rectangle(x,70,x+width,105,fill=colors[label],outline="");c.create_text(x+width/2,55,text=f"{label}: {count}");x+=width+30
        c.create_text(15,18,anchor="w",text="Riesgo residual por nivel (probabilidad residual × impacto residual)",fill="#172033")

    def select_risk(self,event=None):
        selected=self.risk_tree.selection()
        if not selected:return
        try:
            d=next((r for r in self.store.list("riesgos_eticos") if str(r["_id"])==selected[0]),None)
            if not d:return
            values=[d.get("modulo",""),d.get("descripcion",""),d.get("categoria","otro"),str(d.get("probabilidad",1)),str(d.get("impacto",1)),d.get("mitigacion","")]
            for var,value in zip(self.risk_vars,values):var.set(value)
        except Exception as exc:self.db_error(exc)

    def clear_risk(self):
        self.risk_tree.selection_remove(self.risk_tree.selection())
        for var in self.risk_vars:var.set("")

    def edit_risk(self):
        selected=self.risk_tree.selection()
        if not selected:messagebox.showwarning("Selecciona riesgo","Selecciona un riesgo para editar.");return
        try:
            module,desc,cat,p,i,mit=[v.get().strip() for v in self.risk_vars];p=int(p);i=int(i)
            if not module or not desc or not mit or cat not in ("sesgo","privacidad","transparencia","seguridad","responsabilidad","otro") or not 1<=p<=5 or not 1<=i<=5:raise ValueError("Revisa los campos y las escalas 1–5.")
            doc=self.risk_doc(module,desc,cat,p,i,mit);doc.pop("historico",None);doc.pop("fecha",None)
            self.store.update("riesgos_eticos",selected[0],doc,[{"fecha":datetime.now(timezone.utc).isoformat(),"evento":"Riesgo editado","probabilidad":p,"impacto":i,"puntaje_residual":doc["puntaje_residual"]}]);self.refresh_risks();self.refresh_dashboard()
        except Exception as exc:messagebox.showerror("Validación / MongoDB",str(exc))

    def delete_risk(self):
        sel=self.risk_tree.selection()
        if not sel:return
        try:self.store.delete("riesgos_eticos",sel[0]);self.refresh_risks();self.refresh_dashboard()
        except Exception as exc:self.db_error(exc)

    def build_truth(self):
        ttk.Label(self.truth,text="Simulador de reglas",style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.truth,text="A final = (P ∧ S ∧ ¬Q ∧ certificación vigente) ∧ ¬(R ∧ horario restringido). E conserva P ∧ (R ∨ Q). B = R ∧ horario restringido bloquea acceso estándar por política de seguridad.").pack(anchor="w",pady=8)
        self.truth_vars={}
        frm=ttk.Frame(self.truth);frm.pack(anchor="w")
        for i,(k,label) in enumerate([("P","Autorización"),("Q","Sobrepeso"),("R","Material peligroso"),("S","Certificación conductor"),("cert","Vigencia certificación"),("horario","Horario restringido")]):
            v=tk.BooleanVar(value=False);self.truth_vars[k]=v;ttk.Checkbutton(frm,text=label,variable=v,command=self.update_truth).grid(row=i//3,column=i%3,sticky="w",padx=10,pady=8)
        self.truth_result=ttk.Label(self.truth,text="",font=("Segoe UI",16,"bold"));self.truth_result.pack(anchor="w",pady=12)
        ttk.Button(self.truth,text="Exportar tabla de verdad CSV (64 filas)",command=self.export_truth).pack(anchor="w");self.update_truth()

    def update_truth(self):
        f=self.truth_vars;o=evaluate(f["P"].get(),f["Q"].get(),f["R"].get(),f["S"].get(),f["cert"].get(),f["horario"].get())
        self.truth_result.config(text=f"A base = {int(o['A'])}    |    A final = {int(o['A_final'])}    |    E = {int(o['E'])}    |    B = {int(o['B'])}\n"+"\n".join(o["explicacion"]))

    def export_truth(self):
        path=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV","*.csv")])
        if not path:return
        rows=truth_rows()
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
        messagebox.showinfo("Exportación",f"64 combinaciones guardadas en {path}")

    def build_config(self):
        ttk.Label(self.config,text="Configuración",style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.config,text="Modelo Ollama").pack(anchor="w",pady=(16,4));self.model_var=tk.StringVar(value=self.model);ttk.Entry(self.config,textvariable=self.model_var,width=35).pack(anchor="w")
        ttk.Label(self.config,text="URI MongoDB").pack(anchor="w",pady=(16,4));ttk.Label(self.config,text=MONGO_URI).pack(anchor="w")
        ttk.Button(self.config,text="Aplicar modelo",command=self.apply_config).pack(anchor="w",pady=10)
        ttk.Label(self.config,text="Umbral mínimo para marcar revisión humana").pack(anchor="w",pady=(12,3))
        self.review_var=tk.StringVar(value=self.review_threshold);ttk.Combobox(self.config,textvariable=self.review_var,values=PRIORITY,state="readonly",width=18).pack(anchor="w")
        self.simulate_var=tk.BooleanVar(value=True);ttk.Checkbutton(self.config,text="Simular recepción / aviso por correo (no envía mensajes)",variable=self.simulate_var).pack(anchor="w",pady=12)
        ttk.Label(self.config,text="Para activar MongoDB: instala MongoDB Community Server, inicia el servicio y abre esta app. Compass es el cliente visual; conecta con mongodb://localhost:27017.\nPara activar Ollama: instala Ollama, descarga un modelo (ollama pull llama3.2) y usa ese nombre aquí.\nPara cambiar MongoDB URI, define MONGODB_URI antes de ejecutar. El correo está en modo simulación; no se envía correo.",wraplength=850).pack(anchor="w",pady=14)

    def apply_config(self):
        self.model=self.model_var.get().strip() or MODEL
        self.review_threshold=self.review_var.get() if self.review_var.get() in PRIORITY else "alta"
        self.simulate_email=self.simulate_var.get()
        messagebox.showinfo("Configuración aplicada",f"Modelo: {self.model}\nUmbral revisión: {self.review_threshold}\nCorreo en simulación: {'sí' if self.simulate_email else 'no (envío real no configurado)'}")

    def build_admin(self):
        ttk.Label(self.admin,text="Administrador CRUD para las cinco colecciones",style="Title.TLabel").pack(anchor="w")
        bar=ttk.Frame(self.admin);bar.pack(fill="x",pady=8)
        self.admin_collection=tk.StringVar(value="camiones")
        ttk.Combobox(bar,textvariable=self.admin_collection,values=["camiones","accesos","incidentes","riesgos_eticos","evaluaciones_llm"],state="readonly",width=22).pack(side="left")
        ttk.Button(bar,text="Consultar / refrescar",command=self.refresh_admin).pack(side="left",padx=5)
        self.admin_tree=ttk.Treeview(self.admin,columns=("id","vista"),show="headings",height=9);self.admin_tree.heading("id",text="ObjectId");self.admin_tree.heading("vista",text="Vista previa del documento");self.admin_tree.column("id",width=230);self.admin_tree.column("vista",width=760);self.admin_tree.pack(fill="x",pady=5);self.admin_tree.bind("<<TreeviewSelect>>",self.select_admin_doc)
        ttk.Label(self.admin,text="Documento JSON (para alta o edición)").pack(anchor="w")
        self.admin_json=tk.Text(self.admin,height=10,wrap="none");self.admin_json.pack(fill="both",expand=True)
        actions=ttk.Frame(self.admin);actions.pack(fill="x",pady=5)
        ttk.Button(actions,text="Nuevo / limpiar",command=lambda:self.admin_json.delete("1.0","end")).pack(side="left")
        ttk.Button(actions,text="Insertar / guardar edición",command=self.save_admin_doc).pack(side="left",padx=5)
        ttk.Button(actions,text="Eliminar seleccionado",command=self.delete_admin_doc).pack(side="right")
        self.refresh_admin()

    def refresh_admin(self):
        self.admin_tree.delete(*self.admin_tree.get_children())
        try:
            for doc in self.store.list(self.admin_collection.get(),limit=1000):
                ident=str(doc["_id"]);preview=json.dumps({k:v for k,v in doc.items() if k!="_id"},ensure_ascii=False,default=str)
                self.admin_tree.insert("","end",iid=ident,values=(ident,preview[:500]))
        except Exception as exc:self.db_error(exc)

    def select_admin_doc(self,event=None):
        selection=self.admin_tree.selection()
        if not selection:return
        try:
            doc=next(d for d in self.store.list(self.admin_collection.get(),limit=2000) if str(d["_id"])==selection[0])
            doc.pop("_id",None);self.admin_json.delete("1.0","end");self.admin_json.insert("end",json.dumps(doc,ensure_ascii=False,indent=2,default=str))
        except Exception as exc:self.db_error(exc)

    def save_admin_doc(self):
        try:
            doc=json.loads(self.admin_json.get("1.0","end"))
            if not isinstance(doc,dict):raise ValueError("El JSON raíz debe ser un objeto.")
            if "_id" in doc:raise ValueError("No incluyas _id; el ObjectId se conserva al editar.")
            selection=self.admin_tree.selection()
            if selection:self.store.update(self.admin_collection.get(),selection[0],doc)
            else:self.store.put(self.admin_collection.get(),doc)
            self.refresh_admin();self.refresh_dashboard();self.refresh_incidents();self.refresh_trucks();self.refresh_risks()
            messagebox.showinfo("CRUD MongoDB","Documento guardado.")
        except Exception as exc:messagebox.showerror("Documento no guardado",str(exc))

    def delete_admin_doc(self):
        selection=self.admin_tree.selection()
        if not selection:messagebox.showwarning("Selecciona documento","Selecciona una fila.");return
        if not messagebox.askyesno("Confirmar eliminación","¿Eliminar permanentemente el documento seleccionado?"):return
        try:self.store.delete(self.admin_collection.get(),selection[0]);self.refresh_admin();self.refresh_dashboard();self.refresh_incidents();self.refresh_trucks();self.refresh_risks();self.admin_json.delete("1.0","end")
        except Exception as exc:self.db_error(exc)


if __name__ == "__main__":
    App().mainloop()
