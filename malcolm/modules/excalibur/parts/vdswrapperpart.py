import os

import numpy as np
import h5py as h5
from vdsgen.subframevdsgenerator import SubFrameVDSGenerator

from malcolm.modules.scanning.controllers import RunnableController
from malcolm.core import method_takes, REQUIRED, Part
from malcolm.modules.ADCore.infos import DatasetProducedInfo
from malcolm.modules.builtin.vmetas import StringMeta, NumberMeta
from malcolm.modules.scanpointgenerator.vmetas import PointGeneratorMeta

# Number of points to look ahead of the current id index to account for dropped frames
NUM_DROPPED = 10

@method_takes(
    "name", StringMeta("Name of part"), REQUIRED,
    "dataType", StringMeta("Data type of dataset"), REQUIRED,
    "stripeHeight", NumberMeta("int16", "Height of stripes"), REQUIRED,
    "stripeWidth", NumberMeta("int16", "Width of stripes"), REQUIRED)
class VDSWrapperPart(Part):

    # Constants for class
    RAW_FILE_TEMPLATE = "FEM{}"
    OUTPUT_FILE = "EXCALIBUR"
    CREATE = "w"
    APPEND = "a"
    READ = "r"
    ID = "/entry/NDAttributes/NDArrayUniqueId"
    SUM = "/entry/sum/sum"

    required_nodes = ["/entry/detector", "/entry/sum", "/entry/NDAttributes"]
    set_bases = ["/entry/detector", "/entry/sum"]
    default_node_tree = ["/entry/detector/axes", "/entry/detector/signal",
                         "/entry/sum/axes", "/entry/sum/signal"]

    def __init__(self, params):
        self.params = params
        super(VDSWrapperPart, self).__init__(params.name)

        self.current_idx = None
        self.done_when_reaches = None
        self.generator = None
        self.fems = [1, 2, 3, 4, 5, 6]
        self.vds_path = ""
        self.vds = None
        self.command = []
        self.raw_paths = []
        self.raw_datasets = []
        self.data_type = params.dataType
        self.stripe_height = params.stripeHeight
        self.stripe_width = params.stripeWidth
        
    @RunnableController.Abort
    @RunnableController.Reset
    @RunnableController.PostRunReady
    def abort(self, context):
        self.close_files()

    def close_files(self):
        for file_ in self.raw_datasets + [self.vds]:
            if file_ is not None and file_.id.valid:
                self.log.info("Closing file %s", file_)
                file_.close()
        self.raw_datasets = []
        self.vds = None

    def _create_dataset_infos(self, generator, filename):
        uniqueid_path = "/entry/NDAttributes/NDArrayUniqueId"
        data_path = "/entry/detector/detector"
        sum_path = "/entry/sum/sum"
        generator_rank = len(generator.axes)
        # Create the main detector data
        yield DatasetProducedInfo(
            name="EXCALIBUR.data",
            filename=filename,
            type="primary",
            rank=2 + generator_rank,
            path=data_path,
            uniqueid=uniqueid_path)

        # And the sum
        yield DatasetProducedInfo(
            name="EXCALIBUR.sum",
            filename=filename,
            type="secondary",
            rank=2 + generator_rank,
            path=sum_path,
            uniqueid=uniqueid_path)

        # Add any setpoint dimensions
        for axis in generator.axes:
            yield DatasetProducedInfo(
                name="%s.value_set" % axis, filename=filename,
                type="position_set", rank=1,
                path="/entry/detector/%s_set" % axis, uniqueid="")

    @RunnableController.Configure
    @method_takes(
        "generator", PointGeneratorMeta("Generator instance"), REQUIRED,
        "fileDir", StringMeta("File dir to write HDF files into"), REQUIRED,
        "fileTemplate", StringMeta(
            """Printf style template to generate filename relative to fileDir.
            Arguments are:
              1) %s: EXCALIBUR"""), "%s.h5",
        "fillValue", NumberMeta("int32", "Fill value for stripe spacing"), 0)
    def configure(self, context, completed_steps, steps_to_do, part_info,
                  params):
        print "Configure"
        self.generator = params.generator
        self.current_idx = completed_steps
        self.done_when_reaches = completed_steps + steps_to_do
        self.vds_path = os.path.join(params.fileDir,
                                     params.fileTemplate % self.OUTPUT_FILE)
        raw_file_path = params.fileTemplate % self.RAW_FILE_TEMPLATE.format(1)
        node_tree = list(self.default_node_tree)
        for axis in params.generator.axes:
            for base in self.set_bases:
                node_tree.append(base + "/{}_set".format(axis))
                node_tree.append(base + "/{}_set_indices".format(axis))

        with h5.File(self.vds_path, self.CREATE, libver="latest") as self.vds:
            for node in self.required_nodes:
                self.vds.require_group(node)
            for node in node_tree:
                self.vds[node] = h5.ExternalLink(raw_file_path, node)

            # Create placeholder id and sum datasets
            initial_dims = tuple([1 for _ in params.generator.shape])
            initial_shape = initial_dims + (1, 1)
            max_shape = params.generator.shape + (1, 1)
            self.vds.create_dataset(self.ID, initial_shape,
                                    maxshape=max_shape, dtype="int32")
            self.vds.create_dataset(self.SUM, initial_shape,
                                    maxshape=max_shape, dtype="float64",
                                    fillvalue=np.nan)
        files = [params.fileTemplate % self.RAW_FILE_TEMPLATE.format(fem)
                 for fem in self.fems]
        shape = params.generator.shape + (self.stripe_height, self.stripe_width)

        # Create the VDS using vdsgen
        fgen = SubFrameVDSGenerator(
            params.fileDir,
            prefix=None,
            files=files,
            output=params.fileTemplate % self.OUTPUT_FILE,
            source=dict(shape=shape, dtype=self.data_type),
            source_node="/entry/detector/detector",
            target_node="/entry/detector/detector",
            stripe_spacing=0,
            module_spacing=121,
            fill_value=params.fillValue,
            log_level=1 # DEBUG
        )
        print fgen        
        fgen.generate_vds()

        # Store required attributes
        self.raw_paths = [os.path.abspath(os.path.join(params.fileDir, file_))
                          for file_ in files]

        # Open the VDS
        self.vds = h5.File(
                self.vds_path, self.APPEND, libver="latest", swmr=True)
        # Return the dataset information
        dataset_infos = list(self._create_dataset_infos(
            params.generator, params.fileTemplate % self.OUTPUT_FILE))

        return dataset_infos

    @RunnableController.PostRunArmed
    @RunnableController.Seek
    def seek(self, context, completed_steps, steps_to_do, part_info):
        self.current_idx = completed_steps
        self.done_when_reaches = completed_steps + steps_to_do

    @RunnableController.Run
    @RunnableController.Resume
    def run(self, context, update_completed_steps):
        self.log.info("VDS part running")
        if not self.raw_datasets:
            for path_ in self.raw_paths:
                self.log.info("Waiting for file %s to be created", path_)
                while not os.path.exists(path_):
                    context.sleep(1)
                self.raw_datasets.append(
                    h5.File(path_, self.READ, libver="latest", swmr=True))
            for dataset in self.raw_datasets:
                self.log.info("Waiting for id in file %s", dataset)
                while self.ID not in dataset:
                    context.sleep(0.1)
            # here I should grab the handles to the vds dataset, id and all the swmr datasets and ids.
            if self.vds.id.valid and self.ID in self.vds:
                self.vds.swmr_mode = True
                self.vds_sum = self.vds[self.SUM]
                self.vds_id = self.vds[self.ID]
                self.fems_sum = [ix[self.SUM] for ix in self.raw_datasets] 
                self.fems_id = [ix[self.ID] for ix in self.raw_datasets]
            else:
                self.log.warning("File %s does not exist or does not have a "
                             "UniqueIDArray, returning 0", file_)
                return 0
            
            self.previous_idx = 0
        # does this on every run
        try:
            self.log.info("Monitoring raw files until ID reaches %s",
                          self.done_when_reaches)
            while self.current_idx < self.done_when_reaches: # monitor the output of the vds id. When it counts up then we have finished.
                context.sleep(0.1)  # Allow while loop to be aborted
                self.maybe_update_datasets()

        except Exception as error:
            self.log.exception("Error in run. Message:\n%s", error.message)
            self.close_files()
            raise
        
    def maybe_update_datasets(self):
        self.log.info("VDS: updating")
        shapes = []

        self.log.info("VDS: fems ids: %s", self.fems_id)
        # First update the id datasets and store their shapes
        for id in self.fems_id:
            id.refresh()
            shapes.append(np.array(id.shape))


        self.log.info("Shapes: %s", shapes)
        # Now iterate through the indexes, updating ids and sums if needed
        missed = 0
        for index in self.get_indexes_to_check():
            ids = []
            for i, id in enumerate(self.fems_id):
                if not self.index_in_range(index, shapes[i]):
                    return
                ids.append(id[index])
            min_id = min(ids)
            if min_id > self.current_idx:
                self.update_sum(index)
                self.update_id(index, min_id)
                self.current_idx = min_id
                self.log.info("ID reached: %s", self.current_idx)        
            elif missed > NUM_DROPPED:
                return
            else:
                missed += 1

    def index_in_range(self, index, shape):
        # check the given index is valid for the shape of the array
        in_range = index < np.array(shape)[:len(index)]
        return np.all(in_range) 

    def update_id(self, index, min_id):
        self.vds_id.resize(self.fems_id[0].shape) # source and target are now the same shape
        self.vds_id[index] = min_id
        self.vds_id.flush() # flush to disc

    def update_sum(self, index):
        sums = []
        self.log.info("VDS: Updating sum for %s", index) 
        for s in self.fems_sum:
            s.refresh()
            sums.append(s[index])
        self.log.info("VDS: Sums - %s", sums)
        self.vds_sum.resize(self.fems_sum[0].shape)
        self.vds_sum[index] = sum(sums)
        self.log.info("VDS: Sum should be %s", self.vds_sum[index])
        self.vds_sum.flush()

    def get_indexes_to_check(self):
        # returns the indexes that we should check for updates
        for idx in range(self.current_idx, self.done_when_reaches):
            yield tuple(self.generator.get_point(idx).indexes)

