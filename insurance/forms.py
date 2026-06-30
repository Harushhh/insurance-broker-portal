from django import forms
from .models import ExtractionField, FieldSynonym, MISFile, MappingConfiguration

class ExtractionFieldForm(forms.ModelForm):
    class Meta:
        model = ExtractionField
        fields = ['category', 'field_name', 'is_mandatory', 'extraction_method', 'is_active', 'has_dropdown', 'dropdown_options']
        widgets = {
            'category': forms.TextInput(attrs={'class': 'dash-input', 'placeholder': 'e.g., Motor'}),
            'field_name': forms.TextInput(attrs={'class': 'dash-input', 'placeholder': 'e.g., Insurance Company'}),
            'extraction_method': forms.Select(attrs={'class': 'dash-input'}),
            'dropdown_options': forms.Textarea(attrs={'class': 'dash-input', 'rows': 2, 'placeholder': 'Option 1, Option 2, Option 3...'}),
        }

# ==========================================
# AUTOMATED MIS PAYOUT FORMS
# ==========================================
class MISUploadForm(forms.ModelForm):
    class Meta:
        model = MISFile
        fields = ['uploaded_file']
        widgets = {
            'uploaded_file': forms.FileInput(attrs={'class': 'dash-input', 'accept': '.csv, .xlsx, .xls'})
        }

# Retained strictly for legacy API or Admin access. UI editing is locked.
class MappingConfigurationForm(forms.ModelForm):
    class Meta:
        model = MappingConfiguration
        fields = ['source_table', 'source_column', 'operator', 'target_table', 'target_column', 'is_active', 'order_index']