from django import forms
from .models import ExtractionField, FieldSynonym

class ExtractionFieldForm(forms.ModelForm):
    class Meta:
        model = ExtractionField
        fields = ['category', 'field_name', 'is_mandatory', 'extraction_method', 'is_active', 'has_dropdown']
        widgets = {
            'category': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Motor'}),
            'field_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Insurance Company'}),
            'extraction_method': forms.Select(attrs={'class': 'form-control'}),
        }