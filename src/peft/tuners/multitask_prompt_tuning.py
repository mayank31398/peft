"""This code is adapted for the paper: https://arxiv.org/abs/2303.02861 and constitutes the work done at MIT-IBM Watson Research Lab."""

from dataclasses import dataclass, field
import enum
from typing import Optional, Union
import torch

from ..utils import PeftType
from .prompt_tuning import PromptTuningConfig, PromptEmbedding


class MultitaskPromptTuningInit(str, enum.Enum):
    TEXT = "TEXT"
    RANDOM = "RANDOM"
    # average the prefix and column matrices obtained during source training
    AVERAGE_SOURCE_TASKS = "AVERAGE_SOURCE_TASKS"
    # pick prefix and column matrices for a particular task obtained during source training
    EXACT_SOURCE_TASK = "EXACT_SOURCE_TASK"
    # only use the prompt embeddings trained during source training
    ONLY_SOURCE_SHARED = "ONLY_SOURCE_SHARED"


@dataclass
class MultitaskPromptTuningConfig(PromptTuningConfig):
    prompt_tuning_init: Union[MultitaskPromptTuningInit, str] = field(
        default=MultitaskPromptTuningInit.RANDOM,
        metadata={"help": "How to initialize the prompt tuning parameters"},
    )
    prompt_tuning_init_state_dict_path: Optional[str] = field(
        default=None,
        metadata={"help": "The path of source state dict"},
    )
    prompt_tuning_init_task: Optional[int] = field(default=0, metadata={"help": "source task id for initialization"})
    num_ranks: Optional[int] = field(default=1, metadata={"help": "ranks"})
    num_tasks: Optional[int] = field(default=1, metadata={"help": "number of tasks"})

    def __post_init__(self):
        self.peft_type = PeftType.MULTITASK_PROMPT_TUNING


class MultitaskPromptEmbedding(PromptEmbedding):
    def __init__(self, config: MultitaskPromptTuningConfig, word_embeddings):
        super().__init__(config, word_embeddings)

        self.num_tasks = config.num_tasks
        self.num_ranks = config.num_ranks
        self.num_virtual_tokens = config.num_virtual_tokens
        self.num_transformer_submodules = config.num_transformer_submodules
        self.token_dim = config.token_dim

        total_virtual_tokens = self.num_virtual_tokens * self.num_transformer_submodules

        self.prefix_task_cols = torch.nn.Parameter(
            torch.normal(
                mean=0,
                std=0.02,
                size=(self.num_tasks, total_virtual_tokens, self.num_ranks),
            )
        )
        self.prefix_task_rows = torch.nn.Parameter(
            torch.normal(
                mean=0,
                std=0.02,
                size=(self.num_tasks, self.num_ranks, self.token_dim),
            )
        )

        if config.prompt_tuning_init in [
            MultitaskPromptTuningInit.AVERAGE_SOURCE_TASKS,
            MultitaskPromptTuningInit.EXACT_SOURCE_TASK,
            MultitaskPromptTuningInit.ONLY_SOURCE_SHARED,
        ]:
            if config.prompt_tuning_init_state_dict_path is None:
                raise ValueError(
                    f"prompt_tuning_init_state_dict_path needs to be specified with {config.prompt_tuning_init} init method"
                )

            state_dict: dict = torch.load(
                config.prompt_tuning_init_state_dict_path,
                map_location=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
            )

        if config.prompt_tuning_init in [
            MultitaskPromptTuningInit.AVERAGE_SOURCE_TASKS,
            MultitaskPromptTuningInit.EXACT_SOURCE_TASK,
        ]:
            prefix_task_cols_: torch.Tensor = state_dict["prefix_task_cols"]
            prefix_task_rows_: torch.Tensor = state_dict["prefix_task_rows"]

            if config.prompt_tuning_init == MultitaskPromptTuningInit.AVERAGE_SOURCE_TASKS:
                prefix_task_cols_ = prefix_task_cols_.mean(0, keepdim=True)
                prefix_task_rows_ = prefix_task_rows_.mean(0, keepdim=True)
            elif config.prompt_tuning_init == MultitaskPromptTuningInit.EXACT_SOURCE_TASK:
                prefix_task_cols_ = prefix_task_cols_[config.prompt_tuning_init_task, ...].unsqueeze(0)
                prefix_task_rows_ = prefix_task_rows_[config.prompt_tuning_init_task, ...].unsqueeze(0)

            state_dict = {
                "embedding.weight": state_dict["prompt_embeddings"],
                "prefix_task_cols": prefix_task_cols_,
                "prefix_task_rows": prefix_task_rows_,
            }

            self.load_state_dict(state_dict, strict=True)
        elif config.prompt_tuning_init == MultitaskPromptTuningInit.ONLY_SOURCE_SHARED:
            state_dict = {
                "embedding.weight": state_dict["prompt_embeddings"],
            }

            self.load_state_dict(state_dict, strict=False)

    def forward(self, indices, task_ids):
        assert task_ids is not None, "task_ids cannot be None"

        prompt_embeddings = self.embedding(indices)

        task_cols = torch.index_select(self.prefix_task_cols, 0, task_ids)
        task_rows = torch.index_select(self.prefix_task_rows, 0, task_ids)
        task_prompts = torch.matmul(task_cols, task_rows)

        prompt_embeddings *= task_prompts

        return prompt_embeddings
