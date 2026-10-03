from flask import Blueprint

recipes_bp = Blueprint("recipes", __name__, url_prefix="/v1/recipes")
