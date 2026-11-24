"""AI rulebook pages: the field configurator and synonym editor behind the OCR extraction schema."""
from django.shortcuts import render, redirect, get_object_or_404
from ..models import ExtractionField, FieldSynonym
from ..forms import ExtractionFieldForm
from ..services.activity import EDIT, form_changes, record_activity, snapshot


# -------------------------
# Alias / Field Management Configurator
# -------------------------
def field_configurator(request):
    if request.method == 'POST':
        form = ExtractionFieldForm(request.POST)
        if form.is_valid():
            field = form.save()
            record_activity(
                request.user, EDIT, "ocr.field.created", f"Added the OCR field '{field.field_name}'", target=field,
                details={"field_id": field.pk, "values": snapshot(field)},
            )
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
        removed = {"field_id": pk, "values": snapshot(field),
                   "synonyms": list(field.synonyms.values_list("synonym_text", flat=True))}
        name = field.field_name
        field.delete()
        record_activity(request.user, EDIT, "ocr.field.deleted", f"Deleted the OCR field '{name}'", details=removed)
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
                changes = form_changes(form)
                form.save()
                if changes:
                    record_activity(
                        request.user, EDIT, "ocr.field.edited", f"Edited the OCR field '{field_obj.field_name}'",
                        target=field_obj, details={"field_id": pk, "changes": changes},
                    )
                return redirect('edit_field', pk=pk)

        elif action == 'add_synonym':
            synonym_text = request.POST.get('synonym_text', '').strip()
            if synonym_text:
                _, created = FieldSynonym.objects.get_or_create(extraction_field=field_obj, synonym_text=synonym_text)
                if created:
                    record_activity(
                        request.user, EDIT, "ocr.synonym.added",
                        f"Added a synonym to the OCR field '{field_obj.field_name}'", target=field_obj,
                        details={"field_id": pk, "field": field_obj.field_name, "synonym": synonym_text},
                    )
            return redirect('edit_field', pk=pk)

        elif action == 'delete_synonym':
            syn_id = request.POST.get('synonym_id')
            synonym = FieldSynonym.objects.filter(id=syn_id).first()
            if synonym is not None:
                text = synonym.synonym_text
                synonym.delete()
                record_activity(
                    request.user, EDIT, "ocr.synonym.deleted",
                    f"Removed a synonym from the OCR field '{field_obj.field_name}'", target=field_obj,
                    details={"field_id": pk, "field": field_obj.field_name, "synonym": text},
                )
            return redirect('edit_field', pk=pk)
            
    else:
        form = ExtractionFieldForm(instance=field_obj)
        
    return render(request, 'insurance/edit_field.html', {
        'form': form,
        'field_obj': field_obj
    })
