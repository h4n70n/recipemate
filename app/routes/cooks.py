from flask import Blueprint

cooks_bp = Blueprint("cooks", __name__, url_prefix="/v1/recipes")
