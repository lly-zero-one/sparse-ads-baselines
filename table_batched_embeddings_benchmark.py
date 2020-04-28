import click

import numpy as np
import logging

logging.basicConfig(level=logging.DEBUG)
import sys

import torch
import table_batched_embeddings_ops


def div_round_up(a, b):
    return int((a + b - 1) // b) * b


def get_table_batched_offsets_from_dense(merged_indices):
    (B, T, L) = merged_indices.size()
    lengths = np.ones((B, T)) * L
    flat_lengths = lengths.flatten()
    return (
        merged_indices.int().contiguous().view(-1).cuda(),
        torch.tensor(([0] + np.cumsum(flat_lengths).tolist())).int().cuda(),
    )


def benchmark_torch_function(iters, f, *args):
    f(*args)
    torch.cuda.synchronize()
    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    start_event.record()
    for i in range(iters):
        f(*args)
    end_event.record()
    torch.cuda.synchronize()
    return (start_event.elapsed_time(end_event) * 1.0e-3) / iters


import functools


def benchmark_forward(B, E, T, L, D, iters, fp16, managed):
    logging.basicConfig(level=logging.DEBUG)
    import torch
    import table_batched_embeddings

    cc = table_batched_embeddings_ops.TableBatchedEmbeddingBags(
        T,
        E,
        D,
        optimizer=table_batched_embeddings_ops.Optimizer.APPROX_ROWWISE_ADAGRAD,
        learning_rate=0.1,
        eps=0.1,
        stochastic_rounding=False,
        fp16=fp16,
    ).cuda()
    logging.info(
        f"Embedding parameters: {cc.embedding_weights.numel() / 1.0e9:.2f}GParam"
    )

    def w2(c):
        # return c
        @functools.wraps(c)
        def z(w, o, x, *args):
            c(w, o, x.random_(0, E - 1), *args)

        return z

    def w3(c):
        # return c
        @functools.wraps(c)
        def z(g, w, o, x, *args):
            c(g, w, o, x.random_(0, E - 1), *args)

        return z

    merged_indices = torch.randint(low=0, high=E - 1, size=(B, T, L)).int().cuda()
    (indices, offsets) = get_table_batched_offsets_from_dense(merged_indices)
    assert indices.shape[0] == B * T * L
    assert all(
        l == L for l in (offsets[1:] - offsets[:-1]).detach().cpu().numpy().tolist()
    )
    print(indices.shape, indices.min(), indices.max(), indices)
    y0 = table_batched_embeddings.forward(
        cc.embedding_weights, cc.table_offsets, indices, offsets, L, 1, False
    )
    for BT_block_size in [1, 2, 4, 8, 16, 32, 64, 128]:
        for shmem in [True, False]:
            y = table_batched_embeddings.forward(
                cc.embedding_weights,
                cc.table_offsets,
                indices,
                offsets,
                L,
                BT_block_size,
                shmem,
            )
            torch.testing.assert_allclose(y, y0)

    for BT_block_size in [1, 2, 4, 8, 16, 32, 64, 128]:
        for shmem in [True, False]:
            time_per_iter = benchmark_torch_function(
                iters,
                w2(table_batched_embeddings.forward),
                cc.embedding_weights,
                cc.table_offsets,
                indices,
                offsets,
                L,
                BT_block_size,
                shmem,
            )
            logging.info(
                f"Forward, B: {B} {(BT_block_size, shmem)}, E: {E}, T: {T}, D: {D}, L: {L}, BW: {(2 if fp16 else 4) * B * T * L * D / time_per_iter / 1.0e9: .2f}GB/s, T: {time_per_iter * 1.0e6:.0f}us"
            )

    go = torch.randn_like(y0)

    learning_rate = 0.05
    eps = 0.01
    for BT_block_size in [1, 2, 4, 8, 16, 32]:
        for shmem in [True, False]:
            time_per_iter = benchmark_torch_function(
                iters,
                w3(table_batched_embeddings.backward_sgd),
                go,
                cc.embedding_weights,
                cc.table_offsets,
                indices,
                offsets,
                learning_rate,
                L,
                BT_block_size,
                shmem,
            )

            logging.info(
                f"Backward-SGD, B: {B} {(BT_block_size, shmem)}, E: {E}, T: {T}, D: {D}, L: {L}, BW: {2 * (2 if fp16 else 4) * B * T * L * D / time_per_iter / 1.0e9: .2f}GB/s, T: {time_per_iter * 1.0e6:.0f}us"
            )
    for BT_block_size in [1, 2, 4, 8, 16, 32, ]:
        time_per_iter = benchmark_torch_function(
            iters,
            w3(table_batched_embeddings.backward_approx_adagrad),
            go,
            cc.embedding_weights,
            cc.table_offsets,
            indices,
            offsets,
            cc.optimizer_state,
            learning_rate,
            eps,
            L,
            False,
            BT_block_size,
        )

        logging.info(
            f"Backward-ADAGRAD-nonstochastic, B: {B} ({BT_block_size}), E: {E}, T: {T}, D: {D}, L: {L}, BW: {2 * (2 if fp16 else 4) * B * T * L * D / time_per_iter / 1.0e9: .2f}GB/s, T: {time_per_iter * 1.0e6:.0f}us"
        )
        time_per_iter = benchmark_torch_function(
            iters,
            w3(table_batched_embeddings.backward_approx_adagrad),
            go,
            cc.embedding_weights,
            cc.table_offsets,
            indices,
            offsets,
            cc.optimizer_state,
            learning_rate,
            eps,
            L,
            True,
            BT_block_size,
        )

        logging.info(
            f"Backward-ADAGRAD-stochastic, B: {B} ({BT_block_size}), E: {E}, T: {T}, D: {D}, L: {L}, BW: {2 * (2 if fp16 else 4) * B * T * L * D / time_per_iter / 1.0e9: .2f}GB/s, T: {time_per_iter * 1.0e6:.0f}us"
        )


@click.command()
@click.option("--num-tables", default=64)
@click.option("--num-embeddings", default=int(1e4))
@click.option("--embedding-dim", default=32)
@click.option("--batch-size", default=128)
@click.option("--bag-size", default=32)
@click.option("--iters", default=100)
@click.option("--remote", is_flag=True, default=False)
@click.option("--fp16", is_flag=True, default=False)
@click.option("--managed", is_flag=True, default=False)
def cli(
    num_tables,
    num_embeddings,
    embedding_dim,
    batch_size,
    bag_size,
    iters,
    remote,
    fp16,
    managed,
):
    def f():
        import torch

        benchmark_forward(
            batch_size,
            num_embeddings,
            num_tables,
            bag_size,
            embedding_dim,
            iters,
            fp16,
            managed,
        )

    if remote:
        import submitit

        executor = submitit.AutoExecutor(folder="sparse_embedding_perf")
        executor.update_parameters(
            timeout_min=10, partition="dev", constraint="volta32gb", gpus_per_node=1
        )
        job = executor.submit(f)
        job.wait()
        job.result()
        logging.info("Finished")
        import time

        time.sleep(1)
        print(job.stdout())
        print(job.stderr(), file=sys.stderr)
        logging.info("Finished")
    else:
        f()


if __name__ == "__main__":
    cli()