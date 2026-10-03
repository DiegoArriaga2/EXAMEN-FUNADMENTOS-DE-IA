# Informe técnico — Tutor LLM y LogiSmart

## 1. Objetivos

La primera aplicación presenta fundamentos del LLM mediante un tutor con interfaz gráfica, perfil de sistema de mentor de escritura académica y resumen de historial. La segunda convierte el caso logístico en un centro de control con persistencia MongoDB, motor lógico, clasificación híbrida, asistente RAG y matriz de riesgos.

## 2. Arquitectura

`p02primertutor_llm.py` inicia la GUI de `p02primertutor_gui.py`. LogiSmart separa presentación/coordinación (`logismart_app.py`), dominio (`logismart/domain.py`) y persistencia (`logismart/repository.py`). Tkinter comunica las acciones del operador, el dominio calcula reglas y validación, y el repositorio consulta MongoDB.

## 3. MongoDB

Base por defecto: `logismart`. Colecciones:

- `camiones`: placa, camion_id, empresa, autorización y certificación del conductor.
- `accesos`: P, Q, R, S, certificación vigente, horario restringido, resultados A/E/B/A_final, explicación, operador y fecha.
- `incidentes`: correo original, categoría, prioridad, extracción, resumen, estado, historial y revisión humana.
- `riesgos_eticos`: módulo, descripción, categoría, probabilidad/impacto iniciales y residuales, mitigación e histórico.
- `evaluaciones_llm`: prompt, respuesta, modelo, latencia, coincidencia con reglas, etiqueta real y categorías comparadas en el experimento.

El CRUD genérico de la pestaña CRUD MongoDB permite consulta, alta, edición y eliminación en cada colección. También hay pantallas dedicadas de camiones, incidentes y riesgos. La conexión informa los errores al operador. Compass solo visualiza y administra el servidor local/Atlas.

## 4. Reglas y verdad

Reglas base: `A=P∧S∧¬Q`, `E=P∧(R∨Q)`. Extensión de certificación: `C=vigente`; si C es falsa, se bloquea A_final. Extensión por horario: `B=R∧H`, donde H indica horario restringido; la regla de negocio es `A_final=A∧C∧¬B`, para impedir acceso estándar de carga peligrosa en ese horario. E conserva su regla original y solicita inspección especial. El simulador enumera las `2^6=64` combinaciones y exporta CSV. Para cada acceso se guarda explicación de las premisas.

## 5. Clasificador híbrido y prompt

Prompt de clasificación (resumen): solicitar únicamente JSON con claves categoría, prioridad, entidades y resumen; restringir categorías y prioridades a vocabularios indicados; requerir objeto de entidades con placa, camion_id y ubicación; prohibir invenciones y pedir null cuando falte un dato. Pydantic prohíbe claves adicionales y valida los valores categóricos. Hay dos intentos. Ante fallo se activa clasificador por reglas. En fusión se conserva la mayor prioridad; una categoría de seguridad gana el desempate de seguridad y toda discrepancia marca revisión humana.

La GUI evalúa el CSV con 30 o más correos etiquetados. El operador debe revisar la columna `categoria_real` como etiquetado humano. La ejecución produce exactitud, matriz de confusión para reglas/LLM/híbrido, fallos del LLM, predicción por muestra y latencia promedio/mediana. Adjuntar `resultados_experimento.json` de la ejecución real al informe; no usar métricas inventadas.

## 6. RAG

El asistente extrae placa o ID de la pregunta, consulta primero `accesos` y `camiones`, deriva una representación de hechos desde documentos devueltos, y luego pide a Ollama que explique solo ese contexto. La respuesta cita los ObjectId. Si no se recuperan documentos, responde “No tengo información”. Evidencia validada en la demostración: consulta existente ABC-123-D y consulta inexistente ABC-123-DDJDJ.

## 7. Análisis de riesgos éticos

La app precarga los cuatro riesgos requeridos: alucinación/extracción incorrecta, sesgo ante ortografía informal, privacidad del conductor y dependencia de automatización. El nivel es bajo (1–4), medio (5–9), alto (10–16) o crítico (17–25), por probabilidad × impacto. El riesgo residual usa valores posteriores a mitigación editables. Son juicios de demostración; se deben justificar y revisar con evidencia, no interpretarse como mediciones empíricas. La GUI provee alta, edición, baja, historial y gráfica por nivel residual.

## 8. Evidencias por adjuntar después de ejecutar

1. Captura del tutor conversando y resumen de historial.
2. Compass mostrando una decisión de acceso y un incidente.
3. Caso donde reglas y LLM discrepan, con `requiere_revision_humana=true`.
4. Consulta RAG con fuente y una consulta sin resultados.
5. Gráfica y riesgo antes/después de mitigación.
6. Tabla de verdad CSV y reportes PDF/CSV/JSON.
7. `resultados_experimento.json`, etiquetas revisadas y explicación de errores observados.

## 9. Limitaciones y seguridad

La modalidad de correo es simulación y no envía notificaciones reales. Los registros demostrativos no son evidencia de operación en producción. Usar contenido ficticio o anonimizado para el experimento. No subir secretos de Atlas ni datos personales a Git. La decisión final requiere supervisión humana.
