import json
import spacy
from spacy.matcher import Matcher
import re
import os
import sys

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller.
       Must be defined here or imported, as it's needed to load the model.
    """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        # This path is the root directory of the bundled app.
        base_path = sys._MEIPASS
    except Exception:
        # Not running in PyInstaller bundle, use script's directory
        # Assumes modules/spacymatcher.py is the location
        base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..")) # Go up one dir

    return os.path.join(base_path, relative_path)

class PatternMatcher:
    def __init__(self, patterns):
        model_name = "en_core_web_md"

        # Check if running as PyInstaller bundle
        if hasattr(sys, '_MEIPASS'):
            # Load from bundled path in PyInstaller
            model_path = get_resource_path(model_name)
            try:
                self.nlp = spacy.load(model_path)
            except Exception as e:
                raise IOError(f"Failed to load bundled spaCy model from '{model_path}': {e}")
        else:
            # Load from installed/cached package
            try:
                self.nlp = spacy.load(model_name)
            except Exception as e:
                raise IOError(f"Failed to load spaCy model '{model_name}'. Install with: python -m spacy download {model_name}") from e

        self.patterns = patterns

        # Initialize the Matcher
        self.matcher = Matcher(self.nlp.vocab)

        # Add the patterns to the Matcher
        for pattern in self.patterns:
            self.matcher.add("matching", [pattern])

    @staticmethod
    def is_url_or_email(token):
        url_pattern = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
        email_pattern = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
        return bool(url_pattern.search(token) or email_pattern.search(token))

    def extract_identifier(self, text):
        """
        Extract identifiers from text using SpaCy ML patterns defined in patterns.json.

        Args:
            text (str): The text to search for identifiers

        Returns:
            list: List of extracted identifiers matching the configured patterns.
        """
        doc = self.nlp(text)

        # Find matches using SpaCy matcher and remove duplicates
        spacy_matches = list(set([doc[start:end].text for match_id, start, end in self.matcher(doc)])) if self.matcher(doc) else []

        # Filter out URLs and emails, and matches containing '/'
        filtered_matches = [match for match in spacy_matches if not self.is_url_or_email(match) and '/' not in match]

        if filtered_matches:
            return filtered_matches

        # No matches found
        return []

# Backward-compatibility alias
SpacyMatcher = PatternMatcher
