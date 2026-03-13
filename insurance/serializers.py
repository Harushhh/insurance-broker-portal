from rest_framework import serializers
from .models import RateMaster

class RateMasterSerializer(serializers.ModelSerializer):
    # These extra fields turn IDs (like product=1) into actual text (product="Private Car")
    # making it much easier for the external system to understand your data.
    product_name = serializers.CharField(source='product.name', read_only=True)
    sub_product_name = serializers.CharField(source='sub_product.name', read_only=True)
    fuel_type_name = serializers.CharField(source='fuel_type.name', read_only=True)
    make_model_class_name = serializers.CharField(source='make_model_class.name', read_only=True)

    class Meta:
        model = RateMaster
        fields = '__all__' # This shares all columns.