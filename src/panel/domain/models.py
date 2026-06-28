"""Domain / response models: Pydantic white-list base class.

All JSON responses served by the API MUST use a model that inherits from
PublicModel.  PublicModel enforces:

  1. extra="forbid"  — extra fields are rejected, not silently ignored.
  2. Documented naming taboo: subclasses MUST NOT declare fields whose names
     match the following patterns (they carry credential semantics):

       Pattern             Examples
       *secret*            azure_client_secret, secret_value
       *token*             access_token, bearer_token
       *key*               api_key, private_key, ssh_key_path
       *password*          db_password, root_password
       *private_*          private_ip (debatable — prefer explicit allow-listing)
       ssh_key_path        exact name (SSH private-key path)

  If a module DOES need to surface a path reference (e.g. showing the *name*
  of a secret file without its content), create a renamed field with a safe
  alias (e.g. `azure_secret_configured: bool`).

  DB row → response model conversion MUST use explicit field mapping.
  Do NOT use `**row_dict` to construct a PublicModel subclass.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PublicModel(BaseModel):
    """Base class for all outward-facing JSON response models.

    Constraints
    -----------
    * extra="forbid": prevents accidental inclusion of undeclared fields.
    * Subclasses declare only the fields that are safe to expose publicly.
    * Credential-named fields (see module docstring) are PROHIBITED in subclasses.

    Example::

        class HealthResponse(PublicModel):
            status: str
            db: str
            time: str
    """

    model_config = ConfigDict(extra="forbid")
