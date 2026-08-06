import unittest
import json
from app import app

class TestResolverAPI(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True

    def test_root(self):
        """Test that the index endpoint returns service status."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["service"], "WFO Plant Name Synonym Resolver API")
        self.assertEqual(data["status"], "online")

    def test_resolve_by_id_path(self):
        """Test resolving name by WFO ID using path parameter."""
        response = self.client.get('/api/resolve/wfo-0001302011')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["matches"][0]["record"]["id"], "wfo-0001302011")
        self.assertEqual(data["matches"][0]["record"]["scientificname"], "Cyperus violifolia")

    def test_resolve_by_id_query(self):
        """Test resolving name by WFO ID using query parameter."""
        response = self.client.get('/api/resolve?id=wfo-0001302011')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["matches"][0]["record"]["id"], "wfo-0001302011")

    def test_resolve_accepted_name_query(self):
        """Test lookup of an accepted name using query parameters."""
        response = self.client.get('/api/resolve?name=Cyperus%20violifolia&author=Rodriguez%20%26%20Alfonso')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertFalse(data["matches"][0]["is_synonym"])
        self.assertIsNone(data["matches"][0]["accepted_name"])

    def test_resolve_synonym_name_query(self):
        """Test lookup of a synonym name using query parameters."""
        response = self.client.get('/api/resolve?name=Trichophorum%20bracteatum&author=V.Krecz.%20ex%20Czernov')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertTrue(data["matches"][0]["is_synonym"])
        self.assertIsNotNone(data["matches"][0]["accepted_name"])
        self.assertEqual(data["matches"][0]["accepted_name"]["scientificname"], "Trichophorum cespitosum")

    def test_special_characters_preserved(self):
        """Test lookup of Conophytum swanepoelianum subsp. rubrolineatum and verify special characters are preserved."""
        response = self.client.get('/api/resolve?name=Conophytum%20swanepoelianum%20subsp.%20rubrolineatum')
        self.assertEqual(response.status_code, 200)
        
        # Verify raw response bytes do not contain escaped unicode values like \u00e9 or similar
        raw_text = response.data.decode('utf-8')
        self.assertNotIn('\\u', raw_text)
        
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["matches"][0]["record"]["authorship"], "(Rawé) S.A.Hammer")

    def test_resolve_multiple_matches(self):
        """Test resolving a scientific name that has multiple matches (homonyms)."""
        response = self.client.get('/api/resolve?name=Abelia%20rupestris')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(len(data["matches"]), 2)
        
        # Verify both authors are present in the results
        authors = {m["record"]["authorship"] for m in data["matches"]}
        self.assertIn("Lindl.", authors)
        self.assertTrue(any("L.Sp" in a and "th" in a for a in authors if a is not None))
        
        # Now query with a specific author
        response = self.client.get('/api/resolve?name=Abelia%20rupestris&author=Lindl.')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["record"]["authorship"], "Lindl.")

    def test_resolve_not_found(self):
        """Test response when a name does not exist in the database."""
        response = self.client.get('/api/resolve?name=NonExistentPlantName')
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertFalse(data["match_found"])

    def test_resolve_by_name_and_incorrect_author(self):
        """Test that if author is provided but doesn't match, we return 404 instead of falling back."""
        response = self.client.get('/api/resolve?name=Cyperus%20violifolia&author=FakeAuthor')
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertFalse(data["match_found"])

    def test_resolve_fuzzy_echinocereus(self):
        """Test fuzzy matching resolves Echinocereus barthelowanus to Echinocereus barthelowianus."""
        response = self.client.get('/api/resolve/fuzzy?name=Echinocereus%20barthelowanus')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertTrue(len(data["matches"]) > 0)
        
        # Check that Echinocereus barthelowianus is in the matches
        match_names = [m["record"]["scientificname"] for m in data["matches"]]
        self.assertIn("Echinocereus barthelowianus", match_names)
        
        # Check that matches are sorted by similarity_score descending
        scores = [m["similarity_score"] for m in data["matches"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_resolve_fuzzy_cleistocactus(self):
        """Test fuzzy matching resolves Cleistocactus varispinus to Cleistocactus variispinus."""
        response = self.client.get('/api/resolve/fuzzy?name=Cleistocactus%20varispinus')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        
        match_names = [m["record"]["scientificname"] for m in data["matches"]]
        self.assertIn("Cleistocactus variispinus", match_names)

    def test_resolve_fuzzy_limit_parameter(self):
        """Test that the limit parameter restricts the number of results returned."""
        response = self.client.get('/api/resolve/fuzzy?name=Echinocereus%20barthelowanus&limit=2')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertTrue(len(data["matches"]) <= 2)

    def test_count_by_name_only(self):
        """Test counting names matching scientific name when no author is provided."""
        response = self.client.get('/api/count?name=Cyperus%20violifolia')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["query"]["name"], "Cyperus violifolia")
        self.assertEqual(data["query"]["author"], "")
        self.assertGreater(data["count"], 0)

    def test_count_by_name_and_matching_author(self):
        """Test counting names matching scientific name and matching author."""
        response = self.client.get('/api/count?name=Cyperus%20violifolia&author=Rodriguez%20%26%20Alfonso')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["query"]["name"], "Cyperus violifolia")
        self.assertEqual(data["query"]["author"], "Rodriguez & Alfonso")
        self.assertGreater(data["count"], 0)

    def test_count_by_name_and_incorrect_author(self):
        """Test that if author is provided but does not match, count is 0."""
        # As with resolve, if author is provided, it must match.
        response = self.client.get('/api/count?name=Cyperus%20violifolia&author=FakeAuthor')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["query"]["name"], "Cyperus violifolia")
        self.assertEqual(data["query"]["author"], "FakeAuthor")
        self.assertEqual(data["count"], 0)

    def test_count_missing_name(self):
        """Test that missing required name query parameter returns 400 Bad Request."""
        response = self.client.get('/api/count')
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("error", data)

    def test_ancestor_genus_to_family(self):
        """Test finding family (Fabaceae) for genus Tephrosia."""
        response = self.client.get('/api/ancestor?name=Tephrosia&rank=family')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["record"]["scientificname"], "Tephrosia")
        self.assertIsNotNone(data["matches"][0]["ancestor"])
        self.assertEqual(data["matches"][0]["ancestor"]["scientificname"], "Fabaceae")
        self.assertEqual(data["matches"][0]["ancestor"]["rank"], "family")

    def test_ancestor_species_to_family(self):
        """Test finding family (Fabaceae) for species Tephrosia abbottiae."""
        response = self.client.get('/api/ancestor?name=Tephrosia%20abbottiae&rank=family')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["record"]["scientificname"], "Tephrosia abbottiae")
        self.assertIsNotNone(data["matches"][0]["ancestor"])
        self.assertEqual(data["matches"][0]["ancestor"]["scientificname"], "Fabaceae")
        self.assertEqual(data["matches"][0]["ancestor"]["rank"], "family")

    def test_ancestor_synonym_to_family(self):
        """Test finding family (Cyperaceae) for synonym Trichophorum bracteatum."""
        response = self.client.get('/api/ancestor?name=Trichophorum%20bracteatum&rank=family')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        # Both matching homonyms are synonyms and map to Cyperaceae family
        self.assertTrue(len(data["matches"]) > 0)
        for match in data["matches"]:
            self.assertEqual(match["record"]["scientificname"], "Trichophorum bracteatum")
            self.assertIsNotNone(match["ancestor"])
            self.assertEqual(match["ancestor"]["scientificname"], "Cyperaceae")

    def test_ancestor_by_id(self):
        """Test finding ancestor by WFO ID directly."""
        response = self.client.get('/api/ancestor?id=wfo-4000037774&rank=family')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["matches"][0]["record"]["id"], "wfo-4000037774")
        self.assertEqual(data["matches"][0]["ancestor"]["scientificname"], "Fabaceae")

    def test_ancestor_missing_rank(self):
        """Test that missing rank parameter returns 400."""
        response = self.client.get('/api/ancestor?name=Tephrosia')
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("error", data)

    def test_ancestor_missing_name_and_id(self):
        """Test that missing name and id returns 400."""
        response = self.client.get('/api/ancestor?rank=family')
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("error", data)

    def test_ancestor_not_found(self):
        """Test that non-existent name returns 404."""
        response = self.client.get('/api/ancestor?name=NonExistentPlantName&rank=family')
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertFalse(data["match_found"])

    def test_ancestor_no_ancestor_at_rank(self):
        """Test that if no ancestor of specified rank exists, ancestor field is null."""
        response = self.client.get('/api/ancestor?name=Tephrosia&rank=species')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertIsNone(data["matches"][0]["ancestor"])

    def test_ancestor_multiple_matches_filter_unplaced(self):
        """Test that when a name has multiple matches, we check which has a taxon record and prioritize/use it."""
        response = self.client.get('/api/ancestor?name=Abacopteris%20philippinarum&rank=family')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        # Should filter to only keep the match with a taxon/synonym record (wfo-0000151437)
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["record"]["id"], "wfo-0000151437")
        self.assertEqual(data["matches"][0]["ancestor"]["scientificname"], "Thelypteridaceae")

if __name__ == '__main__':
    unittest.main()
