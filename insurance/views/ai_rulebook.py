"""AI rulebook pages: the field configurator and synonym editor behind the OCR extraction schema."""
from django.shortcuts import render, redirect, get_object_or_404
from ..models import ExtractionField, FieldSynonym
from ..forms import ExtractionFieldForm


# -------------------------
# Alias / Field Management Configurator
# -------------------------
def field_configurator(request):
    if request.method == 'POST':
        form = ExtractionFieldForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('field_configurator')
    else:
        form = ExtractionFieldForm()

    fields = ExtractionField.objects.prefetch_related('synonyms').order_by('category', 'order_index')

    return render(request, 'insurance/configurator.html', {
        'fields': fields,
        'form': form
    })


def delete_field(request, pk):
    if request.method == 'POST':
        field = get_object_or_404(ExtractionField, pk=pk)
        field.delete()
    return redirect('field_configurator')


# -------------------------
# Edit Field (Configurations & Synonyms)
# -------------------------
def edit_field(request, pk):
    field_obj = get_object_or_404(ExtractionField, pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'update_field':
            form = ExtractionFieldForm(request.POST, instance=field_obj)
            if form.is_valid():
                form.save()
                return redirect('edit_field', pk=pk)
        
        elif action == 'add_synonym':
            synonym_text = request.POST.get('synonym_text', '').strip()
            if synonym_text:
                FieldSynonym.objects.get_or_create(extraction_field=field_obj, synonym_text=synonym_text)
            return redirect('edit_field', pk=pk)
            
        elif action == 'delete_synonym':
            syn_id = request.POST.get('synonym_id')
            FieldSynonym.objects.filter(id=syn_id).delete()
            return redirect('edit_field', pk=pk)
            
    else:
        form = ExtractionFieldForm(instance=field_obj)
        
    return render(request, 'insurance/edit_field.html', {
        'form': form,
        'field_obj': field_obj
    })
