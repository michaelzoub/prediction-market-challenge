"""EWC: Elastic Weight Consolidation — penalize weight drift using diagonal Fisher information.

Loss = task_loss + λ * Σ F_i * (θ_i − θ*_i)² where θ* are the parameters at the
end of the previous task and F_i is the diagonal Fisher estimated from replay memory.
"""

import torch
from torch import Tensor

CONTRIBUTOR = "anonymous"

_state: dict = {}


def my_method_loss(
    model,
    batch: dict,
    memory_batch: dict | None = None,
    lam: float = 0.1,
    **kw,
) -> Tensor:
    """EWC loss: task_loss + λ * Σ F_i * (θ_i − θ*_i)²."""

    out = model(
        input_ids=batch["input_ids"],
        attention_mask=batch["attention_mask"],
        labels=batch["labels"],
    )
    task_loss = out.loss

    if memory_batch is None and "theta_star" not in _state:
        return task_loss

    if memory_batch is not None and "theta_star" not in _state:
        _state["theta_star"] = {
            n: p.detach().clone()
            for n, p in model.named_parameters()
            if p.requires_grad
        }
        _state["fisher"] = {}
        _state["fisher_n"] = 0

    if memory_batch is not None and _state.get("fisher_n", 0) < 10:
        mem_out = model(
            input_ids=memory_batch["input_ids"],
            attention_mask=memory_batch["attention_mask"],
            labels=memory_batch["labels"],
        )
        params = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
        grads = torch.autograd.grad(
            mem_out.loss,
            [p for _, p in params],
            retain_graph=False,
            create_graph=False,
        )
        for (n, _), g in zip(params, grads):
            sq = g.detach().float().pow(2)
            _state["fisher"][n] = _state["fisher"].get(n, 0) + sq
        _state["fisher_n"] += 1

    theta_star = _state.get("theta_star")
    fisher = _state.get("fisher")
    if not theta_star or not fisher:
        return task_loss

    ewc_lambda = 400.0
    n_samples = max(1, _state["fisher_n"])
    penalty = torch.tensor(0.0, device=task_loss.device)

    for name, param in model.named_parameters():
        if not param.requires_grad or name not in theta_star:
            continue
        diff = param.float() - theta_star[name].float()
        f_diag = fisher[name] / n_samples
        penalty = penalty + (f_diag * diff.pow(2)).sum()

    return task_loss + ewc_lambda * penalty
