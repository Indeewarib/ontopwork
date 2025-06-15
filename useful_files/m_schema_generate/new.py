from rdflib import Graph, RDF, RDFS, OWL, URIRef
from typing import Optional
from sqlalchemy import create_engine, MetaData, select
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus
import json
import sys

from m_schema import MSchema  # Your existing MSchema class


# ---------- TTL Schema Parser ---------- #
class TTLSchemaEngine:
    def __init__(self, ttl_path: str, db_name: str = ""):
        self._graph = Graph()
        self._graph.parse(ttl_path, format="ttl")
        self._mschema = MSchema(db_id=db_name, schema=None)
        self._object_properties = []
        self.init_mschema()

    @property
    def mschema(self) -> MSchema:
        return self._mschema

    def _get_label(self, uri: URIRef) -> str:
        if isinstance(uri, URIRef):
            return uri.split('#')[-1] if '#' in uri else uri.split('/')[-1]
        return str(uri)

    def init_mschema(self):
        # Parse OWL Classes with comments
        for cls in self._graph.subjects(RDF.type, OWL.Class):
            class_name = self._get_label(cls)
            comment = self._graph.value(cls, RDFS.comment)
            comment_str = str(comment) if comment else ""
            self._mschema.add_table(class_name, fields={}, comment=comment_str)

        # Parse DatatypeProperties
        for prop in self._graph.subjects(RDF.type, OWL.DatatypeProperty):
            domain = self._graph.value(prop, RDFS.domain)
            range_ = self._graph.value(prop, RDFS.range)
            if domain is not None:
                class_name = self._get_label(domain)
                field_name = self._get_label(prop)
                field_type = self._get_label(range_) if range_ else "Unknown"
                comment = self._graph.value(prop, RDFS.comment)
                comment_str = str(comment) if comment else ""
                self._mschema.add_field(
                    class_name,
                    field_name,
                    field_type=field_type,
                    primary_key=False,
                    nullable=True,
                    default=None,
                    autoincrement=False,
                    comment=comment_str,
                    examples=[]
                )

        # Parse ObjectProperties
        for prop in self._graph.subjects(RDF.type, OWL.ObjectProperty):
            domain = self._graph.value(prop, RDFS.domain)
            range_ = self._graph.value(prop, RDFS.range)
            comment = self._graph.value(prop, RDFS.comment)
            prop_info = {
                "name": self._get_label(prop),
                "domain": self._get_label(domain) if domain else "Unknown",
                "range": self._get_label(range_) if range_ else "Unknown",
                "comment": str(comment) if comment else ""
            }
            self._object_properties.append(prop_info)


# ---------- Database Example Extractor ---------- #
def add_examples_from_db(mschema: MSchema, sql_engine: Engine, max_values=10, exclude_fields=None):
    if exclude_fields is None:
        exclude_fields = ['descr_it', 'description', 'geom', 'geom_path', 'kml_path']

    metadata = MetaData()
    metadata.reflect(bind=sql_engine)
    db_tables = metadata.tables.keys()

    ontology_to_db = {
        "Art": "art",
        "Location": "location",
        "ArtCategory": "art_category",
        "Calendar": "calendar",
        "Event": "event",
        "EventCategory": "event_category",
        "State": "d_art_e_stato",
        "Tour": "tour",
        "Type": "d_tour_e_tipoit"
    }

    ontology_column_to_db_column = {
        "Tour": {"tourclassid": "classid", "tourdescr_it": "descr_it", "tourimage_url": "image_url", "tourname_it": "name_it"},
        "Art": {"artclassid": "classid", "artdescr_it": "descr_it", "artimage_url": "image_url", "artname_it": "name_it"},
        "Event": {"eventclassid": "classid", "eventdescr_it": "descr_it", "eventimage_url": "image_url", "eventname_it": "name_it"},
        "ArtCategory": {"artcatclassid": "classid", "artcatname_it": "name_it"},
        "EventCategory": {"eventcatclassid": "classid", "eventcatname_it": "name_it"},
        "Calendar": {"calendarclassid": "classid"},
        "Location": {"locationclassid": "classid"},
        "State": {"statecode": "code", "statename": "name"},
        "Type": {"typecode": "code", "typename": "name"}
    }

    with sql_engine.connect() as conn:
        for ont_table, table_info in mschema.tables.items():
            db_table = ontology_to_db.get(ont_table, ont_table.lower())
            if db_table not in db_tables:
                print(f"Skipping: {ont_table} -> DB table '{db_table}' not found")
                continue

            table = metadata.tables[db_table]
            col_map = ontology_column_to_db_column.get(ont_table, {})

            for field_name, field_info in table_info["fields"].items():
                if field_name.lower() in exclude_fields:
                    continue

                db_col = col_map.get(field_name, field_name)
                if db_col not in table.columns:
                    print(f"Skipping: {ont_table}.{field_name} — not matched in DB table '{db_table}'")
                    continue

                try:
                    stmt = select(table.c[db_col]).where(table.c[db_col].isnot(None)).limit(1000)
                    result = conn.execute(stmt)
                    seen = set()
                    examples = []
                    for row in result:
                        val = row[0]
                        if val is None or val in seen:
                            continue
                        seen.add(val)
                        examples.append(val)
                        if len(examples) >= max_values:
                            break
                    field_info["examples"] = examples
                except Exception as e:
                    print(f"Error extracting {ont_table}.{field_name}: {e}")


# ---------- Ontology Summary Printer ---------- #
def print_ontology_summary(ttl_engine: TTLSchemaEngine, file=sys.stdout):
    g = ttl_engine._graph

    def prefixed_name(uri):
        try:
            prefix, namespace, name = g.compute_qname(uri)
            return f"{prefix}:{name}"
        except Exception:
            return str(uri)

    print("# Prefixes", file=file)
    def get_used_prefixes(graph: Graph):
        used_namespaces = set()
    
        # Scan all triples
        for s, p, o in graph:
            for node in (s, p, o):
                if isinstance(node, URIRef):
                    ns = str(node)
                    if "#" in ns:
                        used_namespaces.add(ns.rsplit("#", 1)[0] + "#")
                elif "/" in ns:
                    used_namespaces.add(ns.rsplit("/", 1)[0] + "/")
    
        # Map used namespaces to known prefixes
        prefix_map = {}
        for prefix, ns in graph.namespaces():
            if str(ns) in used_namespaces:
                prefix_map[prefix] = ns
        return prefix_map

# Inside print_ontology_summary:
    used_prefixes = get_used_prefixes(g)
    for prefix, ns in used_prefixes.items():
        print(f"@prefix {prefix}: <{ns}> .", file=file)
    print(file=file)


    print("# Classes", file=file)
    print("[Classes]", file=file)
    for cls in g.subjects(RDF.type, OWL.Class):
        label = ttl_engine._get_label(cls)
        comment = g.value(cls, RDFS.comment)
        comment_str = f" → {comment}" if comment else ""
        print(f"{label} 【Class】{comment_str}", file=file)
    print("", file=file)

    print("# Object Properties", file=file)
    print("[Relationships]", file=file)
    for op in ttl_engine._object_properties:
        prop_name = op['name']
        domain_prefixed = f"ex:{op['domain']}" if op['domain'] != "Unknown" else "Unknown"
        range_prefixed = f"ex:{op['range']}" if op['range'] != "Unknown" else "Unknown"
        comment_text = f'   - "{op["comment"]}"' if op['comment'] else ""

        print(f"{prop_name} 【ObjectProperty】 ({domain_prefixed} → {range_prefixed})", file=file)
        if comment_text:
            print(comment_text, file=file)
        print("   - Example:", file=file)
        print(f"     {domain_prefixed.lower()} ex:{prop_name} {range_prefixed.lower()} .", file=file)
        print("", file=file)

    print("# Datatype Properties", file=file)
    print("[Attributes]", file=file)
    for table_name, table_info in ttl_engine.mschema.tables.items():
        for field_name, field_info in table_info['fields'].items():
            domain = table_name
            range_ = field_info.get('type', 'Unknown')
            comment = field_info.get('comment', '')
            examples = field_info.get('examples', [])
            print(f"{field_name} 【DatatypeProperty】 ({domain} → {range_})", file=file)
            if comment:
                print(f"   - \"{comment}\"", file=file)
            if examples:
                example_str = ', '.join(map(str, examples))
                print(f"   - Example: [{example_str}]", file=file)
            print("", file=file)


# ---------- Main Execution ---------- #
if __name__ == "__main__":
    ttl_engine = TTLSchemaEngine("file:///D:/Masters/paper/art_all.ttl", db_name="my_ontology")
    mschema = ttl_engine.mschema

    user = "postgres"
    password = quote_plus("Indee1997@")
    host = "localhost"
    port = "5432"
    database = "new_db"
    sql_engine = create_engine(f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}")

    add_examples_from_db(
        mschema,
        sql_engine,
        max_values=10,
        exclude_fields=['geom_path', 'kml_path', 'tourdescr_it', 'artdescr_it', 'eventdescr_it', 'proximity_area', 'geom', 'area_di_download']
    )

    print_ontology_summary(ttl_engine)

    with open("ontology_summary.txt", "w", encoding="utf-8") as f:
        print_ontology_summary(ttl_engine, file=f)
