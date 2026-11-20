"""Module A — Data Flow Testing.

Pure function ``deploy_ota_model`` with zero data-flow anomalies
(no dd, du, or ur anomalies): every parameter is read exactly once,
every local variable is defined and then used, and no variable is
used before definition.
"""


def deploy_ota_model(expected_hash, received_hash, model_ram_mb, max_ram_mb, prev_model, new_model):
    """Decide the outcome of an OTA model deployment.

    Parameters
    ----------
    expected_hash : str
        SHA-256 hash stored in the central model registry.
    received_hash : str
        SHA-256 hash computed from the bytes received over the wire.
    model_ram_mb : int
        RAM required by the candidate model (MB).
    max_ram_mb : int
        RAM limit of the target edge node (MB).
    prev_model : object | None
        Identifier of the model currently running on the node.
    new_model : object | None
        Identifier of the candidate model being pushed.

    Returns
    -------
    (status : str, active_model : object | None)
        status is one of:
            "Hash_Error_Discarded", "Rollback_Triggered", "Success"
        active_model is the model the node should run after the decision.
    """
    # Decision 1: hash integrity
    if expected_hash != received_hash:
        return "Hash_Error_Discarded", prev_model

    # Decision 2: hardware capacity (RAM)
    if model_ram_mb > max_ram_mb:
        return "Rollback_Triggered", prev_model

    # Decision 3: clean deployment
    return "Success", new_model
