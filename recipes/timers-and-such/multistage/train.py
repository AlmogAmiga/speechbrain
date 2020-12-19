#!/usr/bin/env/python3
"""

Recipe for "multistage" (speech -> ASR -> text -> NLU -> semantics) SLU.

We transcribe each minibatch using a model trained on LibriSpeech,
then feed the transcriptions into a seq2seq model to map them to semantics.

(The transcriptions could be done offline to make training faster;
the benefit of doing it online is that we can use augmentation
and sample many possible transcriptions.)

(Adapted from the LibriSpeech seq2seq ASR recipe written by Ju-Chieh Chou, Mirco Ravanelli, Abdel Heba, and Peter Plantinga.)

Run using:
> python train.py hparams/train.yaml

Authors
 * Loren Lugosch, Mirco Ravanelli 2020
"""

import sys
import torch
import speechbrain as sb
from speechbrain.utils.data_utils import download_file
from speechbrain.tokenizers.SentencePiece import SentencePiece
from speechbrain.utils.data_utils import undo_padding


# Define training procedure
class SLU(sb.Brain):
    def compute_forward(self, x, y, stage):
        """Forward computations from the waveform batches to the output probabilities."""
        ids, wavs, wav_lens = x
        ids, target_semantics, target_semantics_lens = y
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)

        # Add augmentation if specified
        if stage == sb.Stage.TRAIN:
            if hasattr(self.hparams, "env_corrupt"):
                wavs_noise = self.hparams.env_corrupt(wavs, wav_lens)
                wavs = torch.cat([wavs, wavs_noise], dim=0)
                wav_lens = torch.cat([wav_lens, wav_lens])
                target_semantics = torch.cat(
                    [target_semantics, target_semantics], dim=0
                )
                target_semantics_lens = torch.cat(
                    [target_semantics_lens, target_semantics_lens]
                )
            if hasattr(self.hparams, "augmentation"):
                wavs = self.hparams.augmentation(wavs, wav_lens)

        # Prepare labels
        target_tokens, _ = self.hparams.tokenizer(
            target_semantics,
            target_semantics_lens,
            self.hparams.ind2lab,
            task="encode",
        )
        target_tokens = target_tokens.to(self.device)
        y_in = sb.data_io.data_io.prepend_bos_token(
            target_tokens, self.hparams.bos_index
        )

        # Forward pass
        words, asr_tokens = self.modules.asr_model.transcribe(
            wavs.detach(), wav_lens
        )

        # Pad examples to have same length.
        max_length = max([len(t) for t in asr_tokens])
        for t in asr_tokens:
            t += [0] * (max_length - len(t))
        asr_tokens = torch.tensor([t for t in asr_tokens])

        # Manage length of predicted tokens
        asr_tokens_lens = torch.tensor(
            [max(len(t), 1) for t in asr_tokens]
        ).float()
        asr_tokens_lens = asr_tokens_lens / asr_tokens_lens.max()

        asr_tokens, asr_tokens_lens = (
            asr_tokens.to(self.device),
            asr_tokens_lens.to(self.device),
        )
        embedded_transcripts = self.hparams.input_emb(asr_tokens)
        encoder_out = self.hparams.slu_enc(embedded_transcripts)
        e_in = self.hparams.output_emb(y_in)
        h, _ = self.hparams.dec(e_in, encoder_out, asr_tokens_lens)

        # Output layer for seq2seq log-probabilities
        logits = self.hparams.seq_lin(h)
        p_seq = self.hparams.log_softmax(logits)

        # Compute outputs
        if (
            stage == sb.Stage.TRAIN
            and self.batch_count % show_results_every != 0
        ):
            return p_seq, asr_tokens_lens
        else:
            p_tokens, scores = self.hparams.beam_searcher(
                encoder_out, asr_tokens_lens
            )
            return p_seq, asr_tokens_lens, p_tokens

    def compute_objectives(self, predictions, targets, stage):
        """Computes the loss (NLL) given predictions and targets."""

        if (
            stage == sb.Stage.TRAIN
            and self.batch_count % show_results_every != 0
        ):
            p_seq, decoded_transcript_lens = predictions
        else:
            p_seq, decoded_transcript_lens, predicted_tokens = predictions

        ids, target_semantics, target_semantics_lens = targets
        target_tokens, target_token_lens = self.hparams.tokenizer(
            target_semantics,
            target_semantics_lens,
            self.hparams.ind2lab,
            task="encode",
        )
        target_tokens = target_tokens.to(self.device)
        target_token_lens = target_token_lens.to(self.device)
        if hasattr(self.hparams, "env_corrupt") and stage == sb.Stage.TRAIN:
            target_tokens = torch.cat([target_tokens, target_tokens], dim=0)
            target_token_lens = torch.cat(
                [target_token_lens, target_token_lens], dim=0
            )

        # Add char_lens by one for eos token
        abs_length = torch.round(target_token_lens * target_tokens.shape[1])

        # Append eos token at the end of the label sequences
        target_tokens_with_eos = sb.data_io.data_io.append_eos_token(
            target_tokens, length=abs_length, eos_index=self.hparams.eos_index
        )

        # Convert to speechbrain-style relative length
        rel_length = (abs_length + 1) / target_tokens_with_eos.shape[1]
        loss_seq = self.hparams.seq_cost(
            p_seq, target_tokens_with_eos, length=rel_length
        )

        # (No ctc loss)
        loss = loss_seq

        if (
            stage != sb.Stage.TRAIN
            or self.batch_count % show_results_every == 0
        ):
            # Decode token terms to words
            predicted_semantics = self.hparams.tokenizer(
                predicted_tokens, task="decode_from_list"
            )

            # Convert indices to words
            target_semantics = undo_padding(
                target_semantics, target_semantics_lens
            )
            target_semantics = sb.data_io.data_io.convert_index_to_lab(
                target_semantics, self.hparams.ind2lab
            )
            for i in range(len(target_semantics)):
                print(" ".join(predicted_semantics[i]).replace("|", ","))
                print(" ".join(target_semantics[i]).replace("|", ","))
                print("")

            if stage != sb.Stage.TRAIN:
                self.wer_metric.append(
                    ids, predicted_semantics, target_semantics
                )
                self.cer_metric.append(
                    ids, predicted_semantics, target_semantics
                )

        return loss

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""
        inputs, targets = batch
        predictions = self.compute_forward(inputs, targets, sb.Stage.TRAIN)
        loss = self.compute_objectives(predictions, targets, sb.Stage.TRAIN)
        loss.backward()
        if self.check_gradients(loss):
            self.optimizer.step()
        self.optimizer.zero_grad()
        self.batch_count += 1
        return loss.detach()

    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        inputs, targets = batch
        predictions = self.compute_forward(inputs, targets, stage=stage)
        loss = self.compute_objectives(predictions, targets, stage=stage)
        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        self.batch_count = 0

        if stage != sb.Stage.TRAIN:

            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.error_rate_computer()

    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of a epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr, new_lr = self.hparams.lr_annealing(stage_stats["WER"])
            sb.nnet.schedulers.update_learning_rate(self.optimizer, new_lr)
            self.hparams.train_logger.log_stats(
                stats_meta={"epoch": epoch, "lr": old_lr},
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            with open(self.hparams.wer_file, "w") as w:
                self.wer_metric.write_stats(w)

    def load_tokenizer(self):
        """Loads the sentence piece tokinizer specified in the yaml file"""
        save_model_path = self.hparams.save_folder + "/tok_unigram.model"
        save_vocab_path = self.hparams.save_folder + "/tok_unigram.vocab"

        if hasattr(self.hparams, "tok_mdl_file"):
            download_file(
                source=self.hparams.tok_mdl_file,
                dest=save_model_path,
                replace_existing=True,
            )
            self.hparams.tokenizer.sp.load(save_model_path)

        if hasattr(self.hparams, "tok_voc_file"):
            download_file(
                source=self.hparams.tok_voc_file,
                dest=save_vocab_path,
                replace_existing=True,
            )


if __name__ == "__main__":

    # Load hyperparameters file with command-line overrides
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])
    with open(hparams_file) as fin:
        hparams = sb.load_extended_yaml(fin, overrides)

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Creating tokenizer must be done after preparation
    # Specify the bos_id/eos_id if different from blank_id
    tokenizer = SentencePiece(
        model_dir=hparams["save_folder"],
        vocab_size=hparams["output_neurons"],
        csv_train=hparams["csv_train"],
        csv_read="semantics",
        model_type=hparams["token_type"],
        character_coverage=1.0,
        num_sequences=10000,
    )
    hparams["tokenizer"] = tokenizer

    # Load index2label dict for decoding
    train_set = hparams["train_loader"]()
    valid_set = hparams["valid_loader"]()
    test_real_set = hparams["test_real_loader"]()
    test_synth_set = hparams["test_synth_loader"]()
    hparams["ind2lab"] = hparams["test_real_loader"].label_dict["semantics"][
        "index2lab"
    ]

    # Brain class initialization
    slu_brain = SLU(
        modules=hparams["modules"],
        opt_class=hparams["opt_class"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )
    slu_brain.load_tokenizer()

    # Training
    show_results_every = 250  # plots results every N iterations
    slu_brain.fit(slu_brain.hparams.epoch_counter, train_set, valid_set)

    # Test
    slu_brain.hparams.wer_file = hparams["output_folder"] + "/wer_test_real.txt"
    slu_brain.evaluate(test_real_set)
    slu_brain.hparams.wer_file = (
        hparams["output_folder"] + "/wer_test_synth.txt"
    )
    slu_brain.evaluate(test_synth_set)
