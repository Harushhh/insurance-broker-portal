from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
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


# ==========================================
# SIGNUP (public, pending-admin-approval)
# ==========================================
class SignupForm(forms.Form):
    full_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': 'Jane Doe', 'autocomplete': 'name'}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'auth-input', 'placeholder': 'jane@example.com', 'autocomplete': 'email'}),
    )
    mobile = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': '9876543210', 'autocomplete': 'tel'}),
    )
    designation = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': 'e.g., Relationship Manager'}),
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'auth-input', 'placeholder': 'Choose a username', 'autocomplete': 'username'}),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={'class': 'auth-input', 'placeholder': '••••••••', 'autocomplete': 'new-password'}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={'class': 'auth-input', 'placeholder': '••••••••', 'autocomplete': 'new-password'}),
    )

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', "Passwords do not match.")
        if password1:
            validate_password(password1)
        return cleaned_data