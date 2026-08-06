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

def resolve_synonym_for_record(cursor, name_rec):
    """
    Given a name record, determine if it is a synonym and find its accepted name if so.
    Returns: (is_synonym, accepted_name_record)
    """
    name_id = name_rec["id"]
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
            
    return is_synonym, accepted_name

def resolve_db_id(wfo_id):
    """
    Query the database to resolve a WFO ID.
    Returns: list of match dictionaries
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
            return []
            
        is_synonym, accepted_name = resolve_synonym_for_record(cursor, name_rec)
        return [{
            "record": name_rec,
            "is_synonym": is_synonym,
            "accepted_name": accepted_name
        }]
    finally:
        if conn:
            conn.close()

def resolve_db_name(scientific_name, authorship=None):
    """
    Query the database to resolve a name and optional authorship.
    Returns: list of match dictionaries
    """
    if not db_pool:
        raise RuntimeError("Database connection pool is not available.")
        
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        
        name_recs = []
        
        # If both scientific_name and authorship are provided, try matching them exactly
        if scientific_name and authorship:
            sql = """
                SELECT id, scientificname, authorship, rank, link, basionymid
                FROM name
                WHERE scientificname = %s 
                  AND (authorship = %s OR (authorship IS NULL AND (%s IS NULL OR %s = '')))
            """
            cursor.execute(sql, (scientific_name, authorship, authorship, authorship))
            name_recs = cursor.fetchall()
        else:
            # If only scientificname is provided, retrieve all matching records
            sql = """
                SELECT id, scientificname, authorship, rank, link, basionymid
                FROM name
                WHERE scientificname = %s
            """
            cursor.execute(sql, (scientific_name,))
            name_recs = cursor.fetchall()
            
        results = []
        for name_rec in name_recs:
            is_synonym, accepted_name = resolve_synonym_for_record(cursor, name_rec)
            results.append({
                "record": name_rec,
                "is_synonym": is_synonym,
                "accepted_name": accepted_name
            })
            
        return results
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
            is_synonym, accepted_name = resolve_synonym_for_record(cursor, cand)
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

def count_db_names(scientific_name, authorship=None):
    """
    Query the database to get count of names matching scientific_name and optional authorship.
    """
    if not db_pool:
        raise RuntimeError("Database connection pool is not available.")
        
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        if scientific_name and authorship:
            sql = """
                SELECT COUNT(*)
                FROM name
                WHERE scientificname = %s 
                  AND (authorship = %s OR (authorship IS NULL AND (%s IS NULL OR %s = '')))
            """
            cursor.execute(sql, (scientific_name, authorship, authorship, authorship))
        else:
            sql = """
                SELECT COUNT(*)
                FROM name
                WHERE scientificname = %s
            """
            cursor.execute(sql, (scientific_name,))
            
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        if conn:
            conn.close()

def find_ancestor_by_taxon_id(cursor, taxon_id, target_rank):
    """
    Given a starting taxon ID, traverse up the hierarchy using a recursive CTE 
    to find the closest ancestor of the specified rank.
    """
    if not taxon_id:
        return None
        
    sql = """
        WITH RECURSIVE taxonomy_ancestors AS (
            # Anchor member: get the starting taxon
            SELECT t.id, t.parentid, t.nameid, 0 AS depth
            FROM taxon t
            WHERE t.id = %s
            
            UNION ALL
            
            # Recursive member: get the parent
            SELECT t.id, t.parentid, t.nameid, ta.depth + 1
            FROM taxon t
            INNER JOIN taxonomy_ancestors ta ON t.id = ta.parentid
            WHERE ta.depth < 100
        )
        SELECT n.id, n.scientificname, n.authorship, n.rank, n.link
        FROM taxonomy_ancestors ta
        JOIN name n ON ta.nameid = n.id
        WHERE LOWER(n.rank) = LOWER(%s) AND ta.depth > 0
        ORDER BY ta.depth ASC
        LIMIT 1
    """
    cursor.execute(sql, (taxon_id, target_rank))
    return cursor.fetchone()

@app.route("/")
def index():
    return jsonify({
        "service": "WFO Plant Name Synonym Resolver API",
        "endpoints": {
            "resolve_by_query": "/api/resolve?name=<scientific_name>&author=<authorship>",
            "resolve_by_id_query": "/api/resolve?id=<wfo_id>",
            "resolve_by_id_path": "/api/resolve/<wfo_id>",
            "resolve_fuzzy": "/api/resolve/fuzzy?name=<fuzzy_name>&threshold=<threshold>&limit=<limit>",
            "count": "/api/count?name=<name>&author=<author>",
            "ancestor": "/api/ancestor?name=<name>&rank=<rank>&author=<author>"
        },
        "status": "online" if db_pool else "database_connection_error"
    })

@app.route("/api/resolve/<path:wfo_id>", methods=["GET"])
def resolve_id_path(wfo_id):
    wfo_id = wfo_id.strip()
    if not wfo_id.lower().startswith("wfo-"):
        return jsonify({"error": "Invalid WFO ID format. Must start with 'wfo-'"}), 400
        
    try:
        matches = resolve_db_id(wfo_id)
        
        if not matches:
            return jsonify({
                "query": {"id": wfo_id},
                "match_found": False,
                "matches": []
            }), 404
            
        return jsonify({
            "query": {"id": wfo_id},
            "match_found": True,
            "matches": matches
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
            matches = resolve_db_id(wfo_id)
            if not matches:
                return jsonify({
                    "query": {"id": wfo_id},
                    "match_found": False,
                    "matches": []
                }), 404
                
            return jsonify({
                "query": {"id": wfo_id},
                "match_found": True,
                "matches": matches
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # 2. Resolve by Name + Author
    name = request.args.get("name", "").strip()
    author = request.args.get("author", "").strip()
    
    if not name:
        return jsonify({"error": "Missing required query parameter: 'name' or 'id'"}), 400
        
    try:
        matches = resolve_db_name(name, author)
        
        if not matches:
            return jsonify({
                "query": {"name": name, "author": author},
                "match_found": False,
                "matches": []
            }), 404
            
        return jsonify({
            "query": {"name": name, "author": author},
            "match_found": True,
            "matches": matches
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/count", methods=["GET"])
def count_query():
    name = request.args.get("name", "").strip()
    author = request.args.get("author", "").strip()
    
    if not name:
        return jsonify({"error": "Missing required query parameter: 'name'"}), 400
        
    try:
        count_val = count_db_names(name, author)
        return jsonify({
            "query": {
                "name": name,
                "author": author
            },
            "count": count_val
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ancestor", methods=["GET"])
def get_ancestor():
    rank = request.args.get("rank", "").strip()
    if not rank:
        return jsonify({"error": "Missing required query parameter: 'rank'"}), 400
        
    wfo_id = request.args.get("id", "").strip()
    name = request.args.get("name", "").strip()
    author = request.args.get("author", "").strip()
    
    if not wfo_id and not name:
        return jsonify({"error": "Missing required query parameter: 'name' or 'id'"}), 400
        
    if not db_pool:
        return jsonify({"error": "Database connection pool is not available."}), 500
        
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        
        matches = []
        if wfo_id:
            cursor.execute(
                "SELECT id, scientificname, authorship, rank, link, basionymid FROM name WHERE id = %s LIMIT 1",
                (wfo_id,)
            )
            name_rec = cursor.fetchone()
            if name_rec:
                is_synonym, accepted_name = resolve_synonym_for_record(cursor, name_rec)
                matches.append({
                    "record": name_rec,
                    "is_synonym": is_synonym,
                    "accepted_name": accepted_name
                })
        else:
            if name and author:
                sql = """
                    SELECT id, scientificname, authorship, rank, link, basionymid
                    FROM name
                    WHERE scientificname = %s 
                      AND (authorship = %s OR (authorship IS NULL AND (%s IS NULL OR %s = '')))
                """
                cursor.execute(sql, (name, author, author, author))
                name_recs = cursor.fetchall()
            else:
                sql = """
                    SELECT id, scientificname, authorship, rank, link, basionymid
                    FROM name
                    WHERE scientificname = %s
                """
                cursor.execute(sql, (name,))
                name_recs = cursor.fetchall()
                
            for name_rec in name_recs:
                is_synonym, accepted_name = resolve_synonym_for_record(cursor, name_rec)
                matches.append({
                    "record": name_rec,
                    "is_synonym": is_synonym,
                    "accepted_name": accepted_name
                })
                
        if not matches:
            query_info = {"id": wfo_id} if wfo_id else {"name": name, "author": author}
            query_info["rank"] = rank
            return jsonify({
                "query": query_info,
                "match_found": False,
                "matches": []
            }), 404
            
        processed_matches = []
        for match in matches:
            name_rec = match["record"]
            is_syn = match["is_synonym"]
            
            start_taxon_id = None
            if is_syn:
                cursor.execute("SELECT taxonid FROM synonym WHERE nameid = %s LIMIT 1", (name_rec["id"],))
                syn_row = cursor.fetchone()
                if syn_row:
                    start_taxon_id = syn_row["taxonid"]
            else:
                cursor.execute("SELECT id FROM taxon WHERE nameid = %s LIMIT 1", (name_rec["id"],))
                taxon_row = cursor.fetchone()
                if taxon_row:
                    start_taxon_id = taxon_row["id"]
                    
            match["start_taxon_id"] = start_taxon_id
            processed_matches.append(match)
            
        # Filter: if there are multiple matches, prioritize and use the ones with a corresponding taxon record
        has_taxon_records = [m for m in processed_matches if m["start_taxon_id"] is not None]
        if len(processed_matches) > 1 and has_taxon_records:
            final_matches = has_taxon_records
        else:
            final_matches = processed_matches
            
        results = []
        for match in final_matches:
            ancestor = None
            start_taxon_id = match["start_taxon_id"]
            
            if start_taxon_id:
                ancestor = find_ancestor_by_taxon_id(cursor, start_taxon_id, rank)
                
            results.append({
                "record": match["record"],
                "is_synonym": match["is_synonym"],
                "accepted_name": match["accepted_name"],
                "ancestor": ancestor
            })
            
        query_info = {"id": wfo_id} if wfo_id else {"name": name, "author": author}
        query_info["rank"] = rank
        
        return jsonify({
            "query": query_info,
            "match_found": True,
            "matches": results
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
