import inspect
from numbers import Number
from typing import Callable, Dict, Iterable, List, Sequence, Union

import torch

import meddlr.ops.complex as cplx
from meddlr.config.config import CfgNode
from meddlr.data.transforms.transform import Normalizer
from meddlr.evaluation.testing import flatten_results_dict
from meddlr.forward import SenseModel
from meddlr.transforms.build import (
    build_iter_func,
    build_scheduler,
    build_transforms,
    seed_tfm_gens,
)
from meddlr.transforms.gen import RandomTransformChoice
from meddlr.transforms.mixins import DeviceMixin, GeometricMixin
from meddlr.transforms.tf_scheduler import SchedulableMixin, TFScheduler
from meddlr.transforms.transform import NoOpTransform, Transform, TransformList
from meddlr.transforms.transform_gen import TransformGen
from meddlr.utils import env


class MRIReconAugmentor(DeviceMixin):
    """
    An augmentation pipeline that manages the organization, generation, and application
    of deterministic and random transforms for MRI reconstruction.

    This class supports both augmentations modeled after the physics of the MRI acquisition process
    (e.g. noise, patient motion) and standard geometric image augmentations (e.g. rotation,
    translation, etc.).

    Physics-driven augmentations are treated as *invariant* transformations.
    Image-based augmentations are treated as *equivariant* transformations.

    Deterministic transformations are those which are applied in the same manner to all data.
    These should be of the type :cls:`Transform`.
    Random transformations are those which are applied randomly to each example or
    batch of examples. These should be of the type :cls:`TransformGen`.

    Note:
        Certain transformations require complex-valued data.
        Unexpected behavior may arise when using real-valued data.

    Reference:
        A Desai, B Gunel, B Ozturkler, et al.
        VORTEX: Physics-Driven Data Augmentations Using Consistency Training
        for Robust Accelerated MRI Reconstruction.
        https://arxiv.org/abs/2111.02549.
    """

    def __init__(
        self,
        tfms_or_gens: Union[TransformList, Sequence[Union[Transform, TransformGen]]],
        aug_sensitivity_maps: bool = True,
        seed: int = None,
        device: torch.device = None,
        apply_mask_after_invariant_tfms: bool = False,
    ) -> None:
        """
        Args:
            aug_sensitivity_maps: Whether to apply equivariant, image-based
                transforms to sensitivity maps. Note, invariant, physics-based
                augmentations are not applied to the maps.
            seed: A random seed for the random number generator.
            device: The device to use for the transforms.
                If ``None``, the device will be determined automatically
                based on the data passed in.
            apply_mask_after_invariant_tfms: Whether to apply the mask after invariant transforms.
                Certain invariant operations (like multi-shot motion) can cause non-sampled kspace
                (i.e. entries with zeros) to have non-zero values. Applying the mask after these
                transforms resets those values to zero.
        """
        if isinstance(tfms_or_gens, TransformList):
            tfms_or_gens = tfms_or_gens.transforms
        self.tfms_or_gens = tfms_or_gens
        self.aug_sensitivity_maps = aug_sensitivity_maps
        self.apply_mask_after_invariant_tfms = apply_mask_after_invariant_tfms

        if device is not None:
            self.to(device)
        if seed is None:
            seed_tfm_gens(self.tfms_or_gens, seed=seed)

    def __call__(
        self,
        kspace: torch.Tensor,
        maps: torch.Tensor = None,
        target: torch.Tensor = None,
        normalizer: Normalizer = None,
        mask: Union[bool, torch.Tensor] = None,
        mask_gen: Callable = None,
        skip_tfm: bool = False,
        apply_mask_after_invariant_tfms: bool = None,
    ):
        """Apply augmentations to the set of recon data.

        Args:
            kspace: A complex-valued tensor of shape ``[batch, height, width, #coils]``.
            maps: A complex-valued tensor of shape ``[batch, height, width, #coils, #maps]``.
                To use single-coil data (i.e. ``#coils == 1``), maps should be a Tensor of all ones.
            target: A complex-valued tensor of shape ``[batch, height, width, #maps]``.
            normalizer: A normalizer to normalize the kspace data after equivariant augmentations.
            mask: Whether to use a mask for the SENSE coil combined image.
                If ``True``, a mask will be determined from the non-zero entries of the kspace data.
                If a tensor, it should be a binary tensor broadcastable to the shape of the kspace
                data.
            mask_gen: A callable that returns undersampled kspace and the undersampling mask.
                This undersampling will occur after image-based, equivariant transformations.
            skip_tfm: Whether to skip applying the transformations.
            apply_mask_after_invariant_tfms: See __init__. This value will override the value
                provided in the constructor.

        Returns:
            Tuple[Dict[str, torch.Tensor], List[Transform], List[Transform]]: A tuple of
                a dictionary of transformed data, a list of deterministic equivariant
                transformations, and a list of deterministic invariant transformations.
        """
        if apply_mask_after_invariant_tfms is None:
            apply_mask_after_invariant_tfms = self.apply_mask_after_invariant_tfms
        if skip_tfm:
            tfms_equivariant, tfms_invariant = [], []
        else:
            # For now, we assume that transforms generated by RandomTransformChoice
            # return random transforms of the same type (equivariant or invariant).
            # We don't have to filter these transforms as a result.
            transform_gens = [
                x.get_transform() if isinstance(x, RandomTransformChoice) else x
                for x in self.tfms_or_gens
            ]
            if any(isinstance(x, (list, tuple)) for x in transform_gens):
                _temp = []
                for t in transform_gens:
                    if isinstance(t, (list, tuple)):
                        _temp.extend(t)
                    else:
                        _temp.append(t)
                transform_gens = _temp

            tfms_equivariant, tfms_invariant = self._classify_transforms(transform_gens)

        use_img = normalizer is not None or len(tfms_equivariant) > 0

        # SENSE coil combination of the image.
        # Note, RSS reconstruction is not currently supported.
        if use_img:
            if mask is True:
                mask = cplx.get_mask(kspace)
            A = SenseModel(maps, weights=mask)
            img = A(kspace, adjoint=True)

        # Apply equivariant transforms to the image.
        if len(tfms_equivariant) > 0:
            img, target, maps = self._permute_data(img, target, maps, spatial_last=True)
            img, target, maps, tfms_equivariant = self._apply_te(
                tfms_equivariant, img, target, maps
            )
            img, target, maps = self._permute_data(img, target, maps, spatial_last=False)

        # Get multi-coil kspace from SENSE combined image.
        if len(tfms_equivariant) > 0:
            A = SenseModel(maps)
            kspace = A(img)

        if mask_gen is not None:
            kspace, mask = mask_gen(kspace)
            img = SenseModel(maps, weights=mask)(kspace, adjoint=True)

        if normalizer:
            normalized = normalizer.normalize(
                **{"masked_kspace": kspace, "image": img, "target": target, "mask": mask}
            )
            kspace = normalized["masked_kspace"]
            target = normalized["target"]
            mean = normalized["mean"]
            std = normalized["std"]
        else:
            mean, std = None, None

        # Apply invariant transforms.
        # Invariant transforms only impact the input (i.e. kspace).
        # However, they may need the sensitivity maps to perform the operation.
        if len(tfms_invariant) > 0:
            if apply_mask_after_invariant_tfms:
                assert mask is not None
            kspace = self._permute_data(kspace, spatial_last=True)
            kspace, tfms_invariant = self._apply_ti(
                tfms_invariant, kspace, maps=self._permute_data(maps, spatial_last=True)
            )
            kspace = self._permute_data(kspace, spatial_last=False)
            if apply_mask_after_invariant_tfms:
                kspace = mask * kspace

        out = {"kspace": kspace, "maps": maps, "target": target, "mean": mean, "std": std}

        for s in self.schedulers():
            s.step(kspace.shape[0])

        return out, tfms_equivariant, tfms_invariant

    def schedulers(self) -> List[TFScheduler]:
        """Returns list of schedulers that are used for all transforms.

        Returns:
            List[TFScheduler]: List of schedulers.
        """
        schedulers = [
            tfm.schedulers() for tfm in self.tfms_or_gens if isinstance(tfm, SchedulableMixin)
        ]
        return [x for y in schedulers for x in y]

    def get_tfm_gen_params(self, scalars_only: bool = True):
        """Get dictionary of scheduler parameters."""
        schedulers: Dict[str, Sequence[TFScheduler]] = {
            type(tfm).__name__: tfm._get_param_values(use_schedulers=True)
            for tfm in self.tfms_or_gens
            if isinstance(tfm, SchedulableMixin)
        }
        params = {}
        for tfm_name, p in schedulers.items():
            p = flatten_results_dict(p, delimiter=".")
            # Filter out values that are not scalars
            p = {f"{tfm_name}/{k}": v for k, v in p.items()}
            if scalars_only:
                p = {k: v for k, v in p.items() if isinstance(v, Number)}
            params.update(p)
        return params

    def _classify_transforms(self, transform_gens):
        tfms_equivariant = []
        tfms_invariant = []
        for tfm in transform_gens:
            if isinstance(tfm, TransformGen):
                tfm_kind = tfm._base_transform
            else:
                tfm_kind = type(tfm)
            assert issubclass(tfm_kind, Transform)

            if issubclass(tfm_kind, GeometricMixin):
                tfms_equivariant.append(tfm)
            else:
                tfms_invariant.append(tfm)
        return tfms_equivariant, tfms_invariant

    def _permute_data(self, *args, spatial_last: bool = False):
        out = []
        if spatial_last:
            for x in args:
                dims = (0,) + tuple(range(3, x.ndim)) + (1, 2)
                out.append(x.permute(dims))
        else:
            for x in args:
                dims = (0, x.ndim - 2, x.ndim - 1) + tuple(range(1, x.ndim - 2))
                out.append(x.permute(dims))
        return out[0] if len(out) == 1 else tuple(out)

    def _apply_te(
        self,
        tfms_equivariant: Iterable[Union[Transform, TransformGen]],
        image: torch.Tensor,
        target: torch.Tensor,
        maps: torch.Tensor,
    ):
        """Apply equivariant transforms.

        These transforms affect both the input and the target.

        Args:
            tfms_equivariant: Equivariant transforms to apply.
            image: The kspace to apply these transformations to.
            target:

        Returns:
            Tuple[torch.Tensor, TransformList]: The transformed kspace and the list
                of deterministic transformations that were applied.
        """
        tfms = []
        for g in tfms_equivariant:
            tfm: Transform = g.get_transform(image) if isinstance(g, TransformGen) else g
            if isinstance(tfm, NoOpTransform):
                continue
            image = tfm.apply_image(image)
            if target is not None:
                target = tfm.apply_image(target)
            if maps is not None and self.aug_sensitivity_maps:
                maps = tfm.apply_maps(maps)
            tfms.append(tfm)
        return image, target, maps, TransformList(tfms, ignore_no_op=True)

    def _apply_ti(
        self,
        tfms_invariant: Iterable[Union[Transform, TransformGen]],
        kspace: torch.Tensor,
        maps: torch.Tensor,
    ):
        """Apply invariant transforms.

        These transforms affect only the input, not the target.

        Args:
            tfms_invariant: Invariant transforms to apply.
            kspace: The kspace to apply these transformations to.

        Returns:
            Tuple[torch.Tensor, TransformList]: The transformed kspace and the list
                of deterministic transformations that were applied.
        """
        tfms = []
        for g in tfms_invariant:
            tfm: Transform = g.get_transform(kspace) if isinstance(g, TransformGen) else g
            if isinstance(tfm, NoOpTransform):
                continue
            # FIXME: maybe we dont want to use the maps here?
            if inspect.signature(tfm.apply_kspace).parameters.get("maps", None) is not None:
                kspace = tfm.apply_kspace(kspace, maps=maps)
            else:
                kspace = tfm.apply_kspace(kspace)
            tfms.append(tfm)
        return kspace, TransformList(tfms, ignore_no_op=True)

    def reset(self):
        """Reset all transformation generators."""
        for g in self.tfms_or_gens:
            if isinstance(g, TransformGen):
                g.reset()
        return self

    def seed(self, value: int):
        """Seed all transformation generators."""
        seed_tfm_gens(self.tfms_or_gens, value)
        return self

    def to(self, device: torch.device) -> "MRIReconAugmentor":
        """Moves transformations to a torch device.

        Args:
            device: The device to move to.

        Returns:
            MRIReconAugmentor: self

        Note:
            This method is called in-place. The returned object is self.
        """
        tfms = [tfm for tfm in self.tfms_or_gens if isinstance(tfm, DeviceMixin)]
        for t in tfms:
            t.to(device)
        return self

    @classmethod
    def from_cfg(
        cls, cfg: CfgNode, aug_kind: str, seed: int = None, device: torch.device = None, **kwargs
    ):
        """Build :cls:`MRIReconAugmentor` from a config.

        TODO (arjundd): Decorate __init__ with @configurable.

        Args:
            cfg: A Config node. See ``meddlr/config/defaults.py`` for a complete list of options.
            aug_kind: The scenario in which these augmentations are being used. One of:
                - ``'aug_train'``: Data augmentation as is done in supervised learning.
                - ``'consistency'``: Data augmentations for consistency training (i.e. VORTEX).
            seed: A random seed to initialize this class.
            device: The device to use for the augmentor.

        Returns:
            MRIReconAugmentor: An augmentor.
        """
        mri_tfm_cfg = None
        assert aug_kind in ("aug_train", "consistency")
        if aug_kind == "aug_train":
            mri_tfm_cfg = cfg.AUG_TRAIN.MRI_RECON
        elif aug_kind == "consistency":
            mri_tfm_cfg = cfg.MODEL.CONSISTENCY.AUG.MRI_RECON

        if seed is None and env.is_repro():
            seed = cfg.SEED

        tfms_or_gens = build_transforms(cfg, mri_tfm_cfg.TRANSFORMS, seed=seed, **kwargs)
        scheduler_p = dict(mri_tfm_cfg.SCHEDULER_P)
        ignore_scheduler = scheduler_p.pop("IGNORE", False)
        if len(scheduler_p) and not ignore_scheduler:
            scheduler_p["params"] = ["p"]
            tfms = [tfm for tfm in tfms_or_gens if isinstance(tfm, TransformGen)]
            for tfm in tfms:
                scheduler = build_scheduler(cfg, scheduler_p, tfm)
                tfm.register_schedulers([scheduler])

        if aug_kind in ("aug_train",) and cfg.DATALOADER.NUM_WORKERS > 0:
            func = build_iter_func(cfg.SOLVER.TRAIN_BATCH_SIZE, cfg.DATALOADER.NUM_WORKERS)
            tfms = [tfm for tfm in tfms_or_gens if isinstance(tfm, SchedulableMixin)]
            for tfm in tfms:
                for s in tfm._schedulers:
                    s._iter_fn = func

        return cls(
            tfms_or_gens,
            aug_sensitivity_maps=mri_tfm_cfg.AUG_SENSITIVITY_MAPS,
            seed=seed,
            device=device,
            apply_mask_after_invariant_tfms=mri_tfm_cfg.APPLY_MASK_AFTER_INVARIANT_TRANSFORMS,
        )
