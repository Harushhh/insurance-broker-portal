import os
import json
import time
import re
from decimal import Decimal
from django.conf import settings
from google import genai
from google.genai import types
from .models import ExtractionField

def auto_reconcile_mis(mis_record):
    """
    Automated Engine: Cross-references the newly extracted AI MIS record
    with the LockedPolicy database to check for expected payout discrepancies.
    """
    try:
        from .models import LockedPolicy
        
        # 1. We need a vehicle number to match against our locked database
        if not mis_record.vehicle_registration_number:
            mis_record.reconciliation_status = 'PENDING'
            mis_record.save(update_fields=['reconciliation_status'])
            return

        # 2. Find the most recent locked policy for this vehicle
        locked_policy = LockedPolicy.objects.filter(
            vehicle_no__iexact=mis_record.vehicle_registration_number,
            status="LOCKED"
        ).order_by('-created_at').first()

        if locked_policy and mis_record.net_premium:
            rate_percentage = Decimal(str(locked_policy.po_rate or 0))
            flat_amount = Decimal(str(locked_policy.po_flat_amount or 0))
            
            # Calculate Expected Payout based on Broker Logic
            expected = (mis_record.net_premium * (rate_percentage / Decimal('100'))) + flat_amount
            mis_record.expected_payout = expected
            
            # Here you would typically compare against an 'actual_payout' field. 
            # For now, if we successfully mapped an expected payout, we'll mark it 
            # as a DISCREPANCY to flag it for the admin's attention in the dashboard.
            if expected > 0:
                mis_record.reconciliation_status = 'DISCREPANCY' 
            else:
                mis_record.reconciliation_status = 'PENDING'
                
            mis_record.save(update_fields=['expected_payout', 'reconciliation_status'])

    except Exception as e:
        print(f"\n❌ RECONCILIATION ERROR: {str(e)}\n")

def safe_json_parse(text):
    """
    Attempts to clean and parse the AI's response text into a JSON object.
    It strips away markdown formatting (like ```json ... ```) and handles trailing commas.
    """
    if not text:
        return {}

    # Strip out the common markdown code block wrappers
    clean_text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    clean_text = re.sub(r"^```\s*", "", clean_text)
    clean_text = re.sub(r"\s*```$", "", clean_text)
    clean_text = clean_text.strip()

    try:
        return json.loads(clean_text)
    except json.JSONDecodeError as first_err:
        # Fallback: Try removing trailing commas before closing braces/brackets, a common AI output error
        clean_text = re.sub(r',\s*([}\]])', r'\1', clean_text)
        try:
            return json.loads(clean_text)
        except json.JSONDecodeError as final_err:
            raise Exception(f"Failed to parse AI output into JSON. Output was: {text[:100]}... Error: {final_err}")

def extract_data_with_gemini(file_path):
    """
    Uploads a document to Gemini, queries the database for active extraction rules,
    and returns a structured JSON dictionary using the modern google.genai SDK.
    """
    # 1. Initialize the Standard GenAI Client
    if not hasattr(settings, 'GEMINI_API_KEY') or not settings.GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY is not configured in settings.")
        
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    
    # 2. Upload the file to Gemini's File API
    try:
        uploaded_file = client.files.upload(file=file_path)
    except Exception as e:
         raise Exception(f"Failed to upload document to Gemini servers: {e}")
    
    # PDFs take a few seconds to process server-side
    try:
        while uploaded_file.state.name == "PROCESSING":
            time.sleep(2)
            uploaded_file = client.files.get(name=uploaded_file.name)
            
        if uploaded_file.state.name == "FAILED":
            raise Exception("Gemini servers failed to process the uploaded document.")
            
        # 3. Dynamically build the prompt schema from the database
        active_fields = ExtractionField.objects.filter(is_active=True).prefetch_related('synonyms')
        
        schema_instructions = []
        
        for field in active_fields:
            # Create a safe JSON key (e.g., "Insurance Company" -> "insurance_company")
            json_key = field.field_name.strip().lower().replace(" ", "_")
            
            # Gather synonyms
            synonyms = [syn.synonym_text for syn in field.synonyms.all()]
            syn_text = f"(Aliases/Synonyms to look for: {', '.join(synonyms)})" if synonyms else ""
            
            # Enforce dropdown options if applicable
            dropdown_text = ""
            if getattr(field, 'has_dropdown', False) and getattr(field, 'dropdown_options', None):
                dropdown_text = f"MUST strictly match one of these exact options: [{field.dropdown_options}]."
                
            instruction = f'- "{json_key}": Extract "{field.field_name}". {syn_text} {dropdown_text}'
            schema_instructions.append(instruction)

        prompt = f"""
        You are an expert insurance document data extractor. 
        Read the attached policy document carefully and extract the following fields into a flat JSON object.
        If a requested field is not found in the document, you MUST return null for that key.
        Do not make up information.
        
        Extraction Rules & Required Keys:
        {chr(10).join(schema_instructions)}
        
        Return ONLY a raw, valid JSON object. Do not include markdown formatting, preambles, or explanations.
        """

        # 4. Call Gemini 2.5 Flash
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                system_instruction="You are a strict data extraction system. You output valid JSON only.",
                response_mime_type="application/json",
                temperature=0.1,  # Low temperature keeps the AI factual and prevents hallucination
            )
        )
        
        # 5. Parse and return the JSON
        return safe_json_parse(response.text)

    finally:
        # 6. SECURITY: Guarantee file deletion from Google's servers, even if parsing fails
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass # Fail silently if the file was already deleted or not found