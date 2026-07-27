import os
import difflib
from flask import Flask, request, jsonify
import mysql.connector
from mysql.connector import pooling, Error
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

app = Flask(__name__)

# Prevent Flask from escaping Unicode characters to ASCII in JSON responses
app.json.ensure_ascii = False
app.config['JSON_AS_ASCII'] = False

# Configure database connection pool
try:
    db_pool = pooling.MySQLConnectionPool(
        pool_name="wfo_pool",
        pool_size=5,
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", 3306)),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", "root"),
        database=os.getenv("MYSQL_DATABASE", "wfo_2026_06"),
        charset="utf8mb4",
        use_unicode=True
    )
    print("Database connection pool initialized successfully.")
except Error as e:
    print(f"Error initializing database connection pool: {e}")
    db_pool = None

def resolve_db_id(wfo_id):
    """
    Query the database to resolve a WFO ID.
    Returns: (name_record, is_synonym, accepted_name_record) or (None, False, None)
    """
    if not db_pool:
        raise RuntimeError("Database connection pool is not available.")
        
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Look up by ID in the name table
        sql = """
            SELECT id, scientificname, authorship, rank, link, basionymid
            FROM name
            WHERE id = %s
            LIMIT 1
        """
        cursor.execute(sql, (wfo_id,))
        name_rec = cursor.fetchone()
        
        if not name_rec:
            return None, False, None
            
        name_id = name_rec["id"]
        
        # Check synonym status
        cursor.execute("SELECT taxonid FROM synonym WHERE nameid = %s LIMIT 1", (name_id,))
        syn_row = cursor.fetchone()
        
        is_synonym = False
        accepted_name = None
        
        if syn_row:
            is_synonym = True
            taxon_id = syn_row["taxonid"]
            
            # Retrieve the nameid of the accepted taxon
            cursor.execute("SELECT nameid FROM taxon WHERE id = %s LIMIT 1", (taxon_id,))
            taxon_row = cursor.fetchone()
            
            if taxon_row:
                accepted_name_id = taxon_row["nameid"]
                # Get the accepted name record from the name table
                cursor.execute(
                    """
                    SELECT id, scientificname, authorship, rank, link, basionymid 
                    FROM name 
                    WHERE id = %s 
                    LIMIT 1
                    """,
                    (accepted_name_id,)
                )
                accepted_name = cursor.fetchone()
                
        return name_rec, is_synonym, accepted_name
    finally:
        if conn:
            conn.close()

def resolve_db_name(scientific_name, authorship=None):
    """
    Query the database to resolve a name and optional authorship.
    Returns: (name_record, is_synonym, accepted_name_record) or (None, False, None)
    """
    if not db_pool:
        raise RuntimeError("Database connection pool is not available.")
        
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        
        name_rec = None
        
        # If both scientific_name and authorship are provided, try matching them exactly
        if scientific_name and authorship:
            sql = """
                SELECT id, scientificname, authorship, rank, link, basionymid
                FROM name
                WHERE scientificname = %s 
                  AND (authorship = %s OR (authorship IS NULL AND (%s IS NULL OR %s = '')))
                LIMIT 1
            """
            cursor.execute(sql, (scientific_name, authorship, authorship, authorship))
            name_rec = cursor.fetchone()
            
            # Fallback: if not found with the author, search just by scientificname
            if not name_rec:
                sql = """
                    SELECT id, scientificname, authorship, rank, link, basionymid
                    FROM name
                    WHERE scientificname = %s
                    LIMIT 1
                """
                cursor.execute(sql, (scientific_name,))
                name_rec = cursor.fetchone()
        else:
            # If only scientificname is provided
            sql = """
                SELECT id, scientificname, authorship, rank, link, basionymid
                FROM name
                WHERE scientificname = %s 
                  AND (authorship IS NULL OR authorship = '')
                LIMIT 1
            """
            cursor.execute(sql, (scientific_name,))
            name_rec = cursor.fetchone()
            
            # Fallback: if still not found, get any match for the scientificname
            if not name_rec:
                sql = """
                    SELECT id, scientificname, authorship, rank, link, basionymid
                    FROM name
                    WHERE scientificname = %s
                    LIMIT 1
                """
                cursor.execute(sql, (scientific_name,))
                name_rec = cursor.fetchone()
                
        if not name_rec:
            return None, False, None
            
        name_id = name_rec["id"]
        
        # Check if this name is a synonym in the synonym table
        cursor.execute("SELECT taxonid FROM synonym WHERE nameid = %s LIMIT 1", (name_id,))
        syn_row = cursor.fetchone()
        
        is_synonym = False
        accepted_name = None
        
        if syn_row:
            is_synonym = True
            taxon_id = syn_row["taxonid"]
            
            # Retrieve the nameid of the accepted taxon
            cursor.execute("SELECT nameid FROM taxon WHERE id = %s LIMIT 1", (taxon_id,))
            taxon_row = cursor.fetchone()
            
            if taxon_row:
                accepted_name_id = taxon_row["nameid"]
                # Get the accepted name record from the name table
                cursor.execute(
                    """
                    SELECT id, scientificname, authorship, rank, link, basionymid 
                    FROM name 
                    WHERE id = %s 
                    LIMIT 1
                    """,
                    (accepted_name_id,)
                )
                accepted_name = cursor.fetchone()
                
        return name_rec, is_synonym, accepted_name
        
    finally:
        if conn:
            conn.close()

def resolve_fuzzy_db_name(full_name, threshold=0.75, limit=5):
    """
    Find names in the database that are fuzzy matches to full_name.
    Filters by the first word (genus) in the database using LIKE.
    """
    if not db_pool:
        raise RuntimeError("Database connection pool is not available.")
        
    full_name = " ".join(full_name.split()).strip()
    if not full_name:
        return []
        
    words = full_name.split()
    genus = words[0]
    
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Retrieve all names starting with genus prefix or matching genus exactly
        sql = """
            SELECT id, scientificname, authorship, rank, link, basionymid
            FROM name
            WHERE scientificname LIKE CONCAT(%s, ' %%') OR scientificname = %s
        """
        cursor.execute(sql, (genus, genus))
        candidates = cursor.fetchall()
        
        results = []
        for cand in candidates:
            sci = cand["scientificname"] or ""
            auth = cand["authorship"] or ""
            
            # Compare input with scientificname alone
            score_sci = difflib.SequenceMatcher(None, full_name.lower(), sci.lower()).ratio()
            
            # Compare input with scientificname + authorship
            if auth:
                full_cand = f"{sci} {auth}"
                score_full = difflib.SequenceMatcher(None, full_name.lower(), full_cand.lower()).ratio()
                score = max(score_sci, score_full)
            else:
                score = score_sci
                
            if score >= threshold:
                results.append((score, cand))
                
        # Sort by score descending
        results.sort(key=lambda x: x[0], reverse=True)
        
        # Take the top N matches
        top_results = results[:limit]
        
        final_matches = []
        for score, cand in top_results:
            name_id = cand["id"]
            
            # Check if synonym
            cursor.execute("SELECT taxonid FROM synonym WHERE nameid = %s LIMIT 1", (name_id,))
            syn_row = cursor.fetchone()
            
            is_synonym = False
            accepted_name = None
            
            if syn_row:
                is_synonym = True
                taxon_id = syn_row["taxonid"]
                
                cursor.execute("SELECT nameid FROM taxon WHERE id = %s LIMIT 1", (taxon_id,))
                taxon_row = cursor.fetchone()
                
                if taxon_row:
                    accepted_name_id = taxon_row["nameid"]
                    cursor.execute(
                        """
                        SELECT id, scientificname, authorship, rank, link, basionymid 
                        FROM name 
                        WHERE id = %s 
                        LIMIT 1
                        """,
                        (accepted_name_id,)
                    )
                    accepted_name = cursor.fetchone()
                    
            final_matches.append({
                "similarity_score": round(score, 3),
                "record": cand,
                "is_synonym": is_synonym,
                "accepted_name": accepted_name
            })
            
        return final_matches
    finally:
        if conn:
            conn.close()

@app.route("/")
def index():
    return jsonify({
        "service": "WFO Plant Name Synonym Resolver API",
        "endpoints": {
            "resolve_by_query": "/api/resolve?name=<scientific_name>&author=<authorship>",
            "resolve_by_id_query": "/api/resolve?id=<wfo_id>",
            "resolve_by_id_path": "/api/resolve/<wfo_id>",
            "resolve_fuzzy": "/api/resolve/fuzzy?name=<fuzzy_name>&threshold=<threshold>&limit=<limit>"
        },
        "status": "online" if db_pool else "database_connection_error"
    })

@app.route("/api/resolve/<path:wfo_id>", methods=["GET"])
def resolve_id_path(wfo_id):
    wfo_id = wfo_id.strip()
    if not wfo_id.lower().startswith("wfo-"):
        return jsonify({"error": "Invalid WFO ID format. Must start with 'wfo-'"}), 400
        
    try:
        name_rec, is_synonym, accepted_name = resolve_db_id(wfo_id)
        
        if not name_rec:
            return jsonify({
                "query": {"id": wfo_id},
                "match_found": False
            }), 404
            
        return jsonify({
            "query": {"id": wfo_id},
            "match_found": True,
            "record": name_rec,
            "is_synonym": is_synonym,
            "accepted_name": accepted_name
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/resolve/fuzzy", methods=["GET"])
def resolve_fuzzy():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing required query parameter: 'name'"}), 400
        
    try:
        threshold = float(request.args.get("threshold", "0.75"))
    except ValueError:
        threshold = 0.75
        
    try:
        limit = int(request.args.get("limit", "5"))
    except ValueError:
        limit = 5
        
    try:
        matches = resolve_fuzzy_db_name(name, threshold=threshold, limit=limit)
        
        return jsonify({
            "query": {
                "name": name,
                "threshold": threshold,
                "limit": limit
            },
            "match_found": len(matches) > 0,
            "matches": matches
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/resolve", methods=["GET"])
def resolve_query():
    wfo_id = request.args.get("id", "").strip()
    
    # 1. Resolve by WFO ID if provided
    if wfo_id:
        try:
            name_rec, is_synonym, accepted_name = resolve_db_id(wfo_id)
            if not name_rec:
                return jsonify({
                    "query": {"id": wfo_id},
                    "match_found": False
                }), 404
                
            return jsonify({
                "query": {"id": wfo_id},
                "match_found": True,
                "record": name_rec,
                "is_synonym": is_synonym,
                "accepted_name": accepted_name
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # 2. Resolve by Name + Author
    name = request.args.get("name", "").strip()
    author = request.args.get("author", "").strip()
    
    if not name:
        return jsonify({"error": "Missing required query parameter: 'name' or 'id'"}), 400
        
    try:
        name_rec, is_synonym, accepted_name = resolve_db_name(name, author)
        
        if not name_rec:
            return jsonify({
                "query": {"name": name, "author": author},
                "match_found": False
            }), 404
            
        return jsonify({
            "query": {"name": name, "author": author},
            "match_found": True,
            "record": name_rec,
            "is_synonym": is_synonym,
            "accepted_name": accepted_name
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
