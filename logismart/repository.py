"""Adaptador de persistencia MongoDB; GUI no ejecuta consultas directamente."""
import os
from pymongo import MongoClient

class MongoStore:
    def __init__(self, uri=None, database=None):
        self.uri=uri or os.getenv("MONGODB_URI","mongodb://localhost:27017")
        self.db_name=database or os.getenv("LOGISMART_DB","logismart")
        self.client=self.db=None; self.error=""
        try:
            self.client=MongoClient(self.uri,serverSelectionTimeoutMS=2500)
            self.client.admin.command("ping")
            self.db=self.client[self.db_name]
            self.db.accesos.create_index("fecha")
            self.db.incidentes.create_index([("categoria",1),("fecha",-1)])
        except Exception as exc:
            self.error=str(exc); self.db=None
    def insert(self, collection, document):
        if self.db is None: raise RuntimeError(self.error or "MongoDB sin conexión")
        return self.db[collection].insert_one(document).inserted_id
    def find(self, collection, query=None, limit=500):
        if self.db is None: raise RuntimeError(self.error or "MongoDB sin conexión")
        return list(self.db[collection].find(query or {}).sort("fecha",-1).limit(limit))
    put=insert
    list=find
    def update(self, collection, ident, fields, history=None):
        if self.db is None: raise RuntimeError(self.error or "MongoDB sin conexión")
        from bson import ObjectId
        update={"$set":fields}
        if history:
            key="historial" if collection=="incidentes" else "historico"
            update["$push"]={key:{"$each":history}}
        return self.db[collection].update_one({"_id":ObjectId(str(ident))},update).modified_count
    def delete(self, collection, ident):
        if self.db is None: raise RuntimeError(self.error or "MongoDB sin conexión")
        from bson import ObjectId
        return self.db[collection].delete_one({"_id":ObjectId(str(ident))}).deleted_count
    def incident_weekly_aggregation(self, query=None):
        if self.db is None: raise RuntimeError(self.error or "MongoDB sin conexión")
        pipeline=[]
        if query:pipeline.append({"$match":query})
        pipeline.extend([{"$group":{"_id":{"categoria":"$categoria","year":{"$isoWeekYear":"$fecha"},"week":{"$isoWeek":"$fecha"}},"cantidad":{"$sum":1}}},{"$sort":{"_id.year":1,"_id.week":1,"_id.categoria":1}}])
        return list(self.db.incidentes.aggregate(pipeline))
