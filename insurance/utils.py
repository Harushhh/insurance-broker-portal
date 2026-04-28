import os
import json
import time
from django.conf import settings
from google import genai
from google.genai import types
from .models import ExtractionField

def extract_data_with_gemini(file_path):
    """
    Uploads a document to Gemini, queries the database for extraction rules,
    and returns a structured JSON dictionary using the new GenAI SDK.
    """
    # 1. Initialize the new standard GenAI Client
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    
    # 2. Upload the file to Gemini's File API
    uploaded_file = client.files.upload(file=file_path)
    
    # PDFs can take a few seconds to process on Google's end
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)
        
    if uploaded_file.state.name == "FAILED":
        raise Exception("Gemini failed to process the document.")

    try:
        # 3. Dynamically build the prompt from your database
        active_fields = ExtractionField.objects.filter(is_active=True).prefetch_related('synonyms')
        
        schema_instructions = []
        json_keys = []
        
        for field in active_fields:
            # Create a safe JSON key (e.g., "Insurance Company" -> "insurance_company")
            json_key = field.field_name.strip().lower().replace(" ", "_")
            json_keys.append(json_key)
            
            # Gather synonyms
            synonyms = [syn.synonym_text for syn in field.synonyms.all()]
            syn_text = f"(Aliases/Synonyms to look for: {', '.join(synonyms)})" if synonyms else ""
            
            # Enforce dropdown options if applicable
            dropdown_text = ""
            if getattr(field, 'has_dropdown', False) and getattr(field, 'dropdown_options', None):
                dropdown_text = f"MUST strictly match one of these options: [{field.dropdown_options}]."
                
            schema_instructions.append(f'- "{json_key}": Extract "{field.field_name}". {syn_text} {dropdown_text}')

        prompt = f"""
        You are an expert insurance document data extractor. 
        Read the attached policy document and extract the following fields into a flat JSON object.
        If a field is not found, return null for that key.
        
        Extraction Rules:
        {chr(10).join(schema_instructions)}
        
        Return ONLY valid JSON.
        """

        # 4. Call Gemini 2.5 Flash (Forcing JSON output via new config)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                system_instruction="You are an insurance document parser. You output strict JSON only.",
                response_mime_type="application/json",
                temperature=0.1  # Low temperature keeps the AI factual
            )
        )
        
        # 5. Parse and return the JSON
        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            # Fallback cleanup just in case the model wraps it in markdown anyway
            clean_text = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)

    finally:
        # 6. SECURITY: Always delete the file from Google's servers after processing
        client.files.delete(name=uploaded_file.name)