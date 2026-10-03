"""Tutor LLM con GUI, personalidad distinta e historial resumido.

Requiere Ollama activo y un modelo descargado, por ejemplo llama3.2.
Ejecutar: py p02primertutor_gui.py
"""
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

try:
    import ollama
except ImportError:
    ollama = None

SYSTEM_PROMPT = """Eres un mentor de escritura académica para estudiantes universitarios.
Ayuda a organizar argumentos, mejorar claridad y proponer preguntas de reflexión.
Responde en español cordial. No resuelvas ejercicios de programación ni actúes como profesor de IA.
Si te piden reescribir, conserva el sentido y explica brevemente los cambios.
"""


class Tutor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Tutor de escritura académica · práctica LLM")
        self.geometry("850x650")
        self.model = "llama3.2"
        self.history = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        top = ttk.Frame(self, padding=12); top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Modelo Ollama:").pack(side="left")
        self.model_var = tk.StringVar(value=self.model)
        ttk.Entry(top, textvariable=self.model_var, width=22).pack(side="left", padx=8)
        ttk.Button(top, text="Aplicar", command=self.apply_model).pack(side="left")
        ttk.Button(top, text="Resumen de mi historial", command=self.summary).pack(side="right")
        self.view = tk.Text(self, wrap="word", state="disabled", padx=12, pady=12)
        self.view.grid(row=1, column=0, sticky="nsew", padx=12)
        bottom = ttk.Frame(self, padding=12); bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.input = tk.Text(bottom, height=4, wrap="word")
        self.input.grid(row=0, column=0, sticky="ew")
        ttk.Button(bottom, text="Enviar", command=self.send).grid(row=0, column=1, padx=(8,0))
        self._write("Sistema", "Tutor de escritura listo. Pregunta cómo organizar, revisar o aclarar un texto.")

    def apply_model(self):
        self.model = self.model_var.get().strip() or "llama3.2"
        self._write("Sistema", f"Modelo configurado: {self.model}")

    def _write(self, who, text):
        self.view.configure(state="normal")
        self.view.insert("end", f"{who}: {text}\n\n")
        self.view.configure(state="disabled"); self.view.see("end")

    def send(self):
        question = self.input.get("1.0", "end").strip()
        if not question: return
        self.input.delete("1.0", "end"); self._write("Tú", question)
        if not ollama:
            messagebox.showerror("Ollama", "Instala la dependencia con py -m pip install ollama."); return
        self.configure(cursor="watch"); self.update_idletasks()
        try:
            messages = [{"role":"system", "content":SYSTEM_PROMPT}] + self.history + [{"role":"user", "content":question}]
            result = ollama.chat(model=self.model, messages=messages)
            answer = result["message"]["content"]
            self.history.extend([{"role":"user","content":question},{"role":"assistant","content":answer}])
            self._write("Tutor", answer)
        except Exception as exc:
            self._write("Sistema", f"No pude consultar Ollama: {exc}")
        finally: self.configure(cursor="")

    def summary(self):
        if not self.history:
            messagebox.showinfo("Historial", "Aún no hay mensajes en esta sesión."); return
        if not ollama:
            summary = "\n".join(m["content"][:100] for m in self.history if m["role"] == "user")
        else:
            try:
                transcript = "\n".join(f"{m['role']}: {m['content']}" for m in self.history)
                result = ollama.chat(model=self.model, messages=[
                    {"role":"system","content":"Resume en español, en 3 viñetas breves, los temas y avances de esta conversación. No agregues información."},
                    {"role":"user","content":transcript}])
                summary = result["message"]["content"]
            except Exception as exc:
                messagebox.showerror("Ollama", str(exc)); return
        messagebox.showinfo("Resumen de tu historial", summary)


if __name__ == "__main__":
    Tutor().mainloop()
